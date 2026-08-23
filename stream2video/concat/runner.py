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
from stream2video.tools import popen_with_retry
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    kill_and_reap,
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
    pre_progress_timeout: int | None = None,
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

    ``memory_monitor`` (optional): when provided, the monitor's
    daemon thread is started AFTER the subprocess is spawned and stopped
    in the finally block. The monitor fires ``cancel_callback`` on a
    hard memory threshold, which routes through the same cancel path
    the user's Ctrl+C uses. None disables the monitor (preserves
    historical behaviour for callers that haven't been updated).
    """
    stdout_target = subprocess.PIPE if track_progress else subprocess.DEVNULL
    # A non-positive timeout used to disable the read-loop deadline but
    # still reach ``_wait_with_cancel`` with ``remaining_timeout=0``,
    # which fired "timeout after 0s" AFTER a successful encode finished.
    # Reject it up front — the config floors (min 1) already protect
    # CLI/GUI callers; this guard is for direct API users.
    if timeout is not None and timeout <= 0:
        raise FFmpegError(f"timeout must be a positive number of seconds, got {timeout!r}")
    # Debug logging to help diagnose spawn failures from real GUI runs. When
    # this exception fires, we want to know exactly what was attempted.
    logger.debug(
        f"spawning ffmpeg: cmd[0]={cmd[0]!r}, "
        f"cmdlen={len(cmd)}, path_exists={Path(cmd[0]).is_file()}, "
        f"cwd={os.getcwd()!r}, shell={os.getenv('COMSPEC', '?')}"
    )
    try:
        # popen_with_retry (parity with download.py): a
        # winget shim / AV filter driver can transiently report
        # FileNotFoundError for a binary that exists on the next
        # spawn attempt; the helper re-resolves the path and retries
        # before surfacing. Tests still intercept at
        # ``stream2video.concat.subprocess.Popen`` because the helper
        # delegates to that exact symbol.
        process = popen_with_retry(
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
            f"executable not found in PATH "
            f"(attempted: {cmd[0]!r}, exists={Path(cmd[0]).is_file()}, "
            f"winerror={getattr(e, 'winerror', '?')}, "
            f"filename={getattr(e, 'filename', '?')!r}, "
            f"strerror={getattr(e, 'strerror', '?')!r})"
        ) from e
    except OSError as e:
        # popen_with_retry re-raises the LAST OSError after exhausting its
        # retries — including the transient-class winerrors 3/206 that
        # arrive as bare OSError (not FileNotFoundError). Without this
        # wrapper those escaped raw instead of as FFmpegError, breaking
        # the caller contract (fallback.py compensates by catching OSError
        # too; every other _run_ffmpeg caller expected FFmpegError).
        logger.error(
            "ffmpeg spawn failed after retries: cmd[0]=%r errno=%s winerror=%s filename=%r",
            cmd[0],
            getattr(e, "errno", "?"),
            getattr(e, "winerror", "?"),
            getattr(e, "filename", "?"),
        )
        raise FFmpegError(
            f"failed to spawn ffmpeg "
            f"(attempted: {cmd[0]!r}, errno={getattr(e, 'errno', '?')}, "
            f"winerror={getattr(e, 'winerror', '?')}, "
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

        # Pre-first-progress stall budget. The full
        # ``stall_kill`` window (default 300s) is meant for *mid-encode*
        # stalls; an ffmpeg that hasn't emitted a single ``out_time_us``
        # line is usually wedged on a broken/corrupt input (or probing
        # a network share) and shouldn't get five minutes before anyone
        # notices. ``pre_progress_timeout=None`` preserves the legacy
        # behaviour (only the stall watchdog guards a silent start);
        # when set, the first progress line must arrive within that
        # budget or the process is killed.
        pre_progress_end: float | None = (
            (time.monotonic() + float(pre_progress_timeout))
            if pre_progress_timeout is not None
            else None
        )
        got_first_progress = False

        # Stall watchdog. The ``track_progress=True`` branch checks
        # ``elapsed_since_progress`` inside its readline loop, but readline
        # blocks until ffmpeg emits a line -- a fully-hung ffmpeg (deadlock,
        # no stdout at all) would never surface as a stall there. This
        # daemon thread polls ``last_progress_time`` independently of
        # stdout availability and kills the process when the stall window
        # expires. The track_progress loop's inline check is retained as
        # a fast path (it kills ASAP after a stalled line arrives).
        #
        # The watchdog is ONLY started in the ``track_progress=True``
        # branch: with ``track_progress=False`` stdout is DEVNULL so no
        # progress line can ever arrive and ``last_progress_time`` stays
        # frozen at spawn — an unconditionally-started watchdog would
        # kill a healthy-but-silent encode (e.g. a per-segment encode
        # whose ``timeout`` of 10 min exceeds the 5 min ``stall_kill``
        # window) despite the docstring promising stall detection only
        # runs when progress is tracked. The wall-clock ``timeout`` still
        # bounds the silent branch via ``_wait_with_cancel`` below.
        #
        # ``stall_killed`` is set BEFORE the kill() so the post-mortem
        # rc-analysis can distinguish "watchdog killed a stalled ffmpeg"
        # from a genuine OOM/SIGKILL (rc -9). Without this, a stall-kill
        # surfaced as rc=-9 to the main loop and looked_like_oom reported
        # "ran out of memory" — the user then chased memory instead of
        # the real cause (P1 audit v0.3 §4).
        stall_stop = threading.Event()
        stall_killed = threading.Event()

        if track_progress:

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

            stall_thread = threading.Thread(
                target=_stall_watchdog, daemon=True, name=f"stall_{label}"
            )
            stall_thread.start()

        # Helper so every kill-first path in the read loop reaps the child
        # before propagating: on Windows ``kill()`` is asynchronous, and letting
        # the exception escape without a ``wait()`` keeps the process handles
        # (and the segment file ffmpeg had open) alive long enough for the next
        # ``shutil.rmtree(seg_dir)`` to trip WinError 32 (file busy). Delegates
        # to the shared ``kill_and_reap`` (utils.py) so the kill+reap policy
        # lives in exactly one place.
        def _kill_and_raise(exc: BaseException) -> None:
            kill_and_reap(process)
            raise exc from None

        try:
            with cancel_monitor(process, _effective_cancel_callback) as cancelled:
                if track_progress:
                    stdout_pipe = process.stdout
                    assert stdout_pipe is not None
                    # Use a queue-based reader so the consumer loop
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
                            _kill_and_raise(
                                FFmpegError(
                                    f"{label} timeout after {int(elapsed_total)}s "
                                    f"(limit {timeout}s — killed mid-encode)"
                                )
                            )
                        try:
                            raw_line = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                        except queue.Empty:
                            # No new line -- check cancel + stall.
                            if _effective_cancel_callback():
                                _kill_and_raise(CancelledError(f"{label} cancelled"))
                            if cancelled.is_set():
                                raise CancelledError(f"{label} cancelled") from None
                            elapsed_since_progress = time.monotonic() - last_progress_time
                            if elapsed_since_progress > stall_kill:
                                _kill_and_raise(
                                    FFmpegError(
                                        f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                        "possible resource exhaustion"
                                    )
                                )
                            if not got_first_progress and (
                                pre_progress_end is not None and time.monotonic() > pre_progress_end
                            ):
                                _kill_and_raise(
                                    FFmpegError(
                                        f"{label} produced no progress at all within "
                                        f"{int(time.monotonic() - process_start)}s of spawn "
                                        "-- likely a wedged input (broken file, blocked read)"
                                    )
                                )
                            elif elapsed_since_progress > stall_warning and not stall_warned:
                                stall_warned = True
                                logger.warning(
                                    f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                                )
                            continue
                        if raw_line is None:
                            break  # EOF -- pipe closed
                        if _effective_cancel_callback():
                            _kill_and_raise(CancelledError(f"{label} cancelled"))
                        if cancelled.is_set():
                            raise CancelledError(f"{label} cancelled") from None
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line.startswith("out_time_us="):
                            got_first_progress = True
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
                            _kill_and_raise(
                                FFmpegError(
                                    f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                    "possible resource exhaustion"
                                )
                            )
                        elif elapsed_since_progress > stall_warning and not stall_warned:
                            stall_warned = True
                            logger.warning(
                                f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                            )

                # The wall-clock timeout is a deadline from SPAWN, not from
                # this wait: the read loop above already consumed part of the
                # budget, so pass only the remaining time. Otherwise a process
                # that closes stdout but refuses to die would get up to
                # 2x timeout before being killed, contradicting the docstring.
                remaining_timeout = timeout
                if timeout > 0:
                    remaining_timeout = max(1, timeout - int(time.monotonic() - process_start))
                _c._wait_with_cancel(process, remaining_timeout, _effective_cancel_callback, label)
                wait_for_drain()
                drain_done = True

                if process.returncode != 0:
                    # Cancel-vs-EOF race: ``cancel_monitor`` kills the child,
                    # its stdout closes, the reader thread queues EOF — and
                    # this loop can BREAK on that EOF before the Empty-path
                    # cancel check ever sees the latched callback. Without
                    # this re-check a user cancel then surfaces as a generic
                    # "ffmpeg failed: unknown error (no stderr)" (observed
                    # consistently on the Windows CI runner, where the kill
                    # lands before the first empty-get). Consult the cancel
                    # state before classifying the non-zero exit as a
                    # failure; memory/stall causes still win below for
                    # genuine kills.
                    if _effective_cancel_callback() or cancelled.is_set():
                        raise CancelledError(f"{label} cancelled") from None
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
                    # Surface OOM as a dedicated error so the CLI/GUI
                    # can hint the user to lower the memory budget or pick
                    # the Low-memory preset, instead of dumping the raw
                    # stderr. SIGKILL on POSIX (rc -9 / 137) or stderr
                    # allocator-failure markers — see looks_like_oom.
                    if _c.looks_like_oom(process.returncode, stderr_text):
                        raise FFmpegOutOfMemoryError(
                            f"{label} ran out of memory (rc={process.returncode}); {_OOM_HINT}"
                        )
                    raise FFmpegError(f"{label} failed: {msg}")

        except CancelledError:
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory (memory monitor hard limit hit); {_OOM_HINT}"
                ) from None
            raise
        except subprocess.TimeoutExpired as e:
            process.kill()
            try:
                # Bounded reap: on Windows TerminateProcess is async and a
                # wedged ffmpeg (network-share I/O, AV lock) would hang an
                # unbounded wait() here forever, stranding the worker
                # thread. Mirrors the timeout bound everywhere else in the
                # pipeline (_kill_and_raise/_wait_with_cancel use 30s).
                process.wait(timeout=30)  # reap so stderr can finish
                # draining and the next rmtree(work_dir) doesn't hit
                # WinError 32.
            except subprocess.TimeoutExpired:
                pass
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory (memory monitor hard limit hit); {_OOM_HINT}"
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
            # Unconditional-reap guard: if every kill() path above failed
            # (AccessDenied, transient handle ownership) the child is
            # still alive here. Closing stdout/stderr below would strand
            # the reader/drain daemon threads in ``readline`` forever
            # (no EOF while any writer exists) and leak one pipe HANDLE
            # per stuck run, exactly the pattern download.py's finally
            # guards against. Best-effort: any failure is already logged
            # by the originating path, so a no-op kill is acceptable.
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
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
    ``subprocess.run`` call with ``check=True, capture_output=True``, this:

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
        # popen_with_retry here as well (parity): the
        # transient winget-shim FNF is just as fatal for the cut phase.
        # Tests still intercept at ``stream2video.concat.subprocess.Popen``
        # (the helper delegates to that symbol).
        process = popen_with_retry(
            cmd,
            # Same rationale as ``_run_ffmpeg``: inheriting a
            # console-mode stdin under pythonw.exe triggers a
            # CreateProcessW winerror 206 on Windows.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        raise FFmpegError(
            f"executable not found in PATH "
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
        try:
            # Bounded reap: on Windows TerminateProcess is async and a
            # wedged child would hang an unbounded wait() (and the
            # finally-drain below) forever. Mirrors the timeout bound
            # used everywhere else in the pipeline (30s).
            process.wait(timeout=30)  # reap so stderr can finish draining
        except subprocess.TimeoutExpired:
            pass  # already dead or un-killable; nothing more to reap
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
                try:
                    process.wait(timeout=30)  # reap — same WinError-32
                    # rationale as the inline stall/cancel paths above;
                    # kill() is async on Windows so a bare wait() could
                    # block forever on a wedged child.
                except subprocess.TimeoutExpired:
                    pass  # already dead or un-killable; nothing more to reap
                raise CancelledError(f"{label} cancelled") from None
