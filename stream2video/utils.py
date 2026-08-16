"""Shared utility functions."""

import logging
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Protocol

from stream2video.tools import ffprobe_path, run_with_retry

logger = logging.getLogger(__name__)

CANCEL_POLL_INTERVAL = 0.5


class WaitForDrain(Protocol):
    """Signature of the callable returned by ``drain_stderr_lines``.

    ``timeout`` bounds how long to block for the drain thread to finish;
    omitting it uses the implementation's default.
    """

    def __call__(self, timeout: float | None = 30.0) -> None: ...


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
                # Re-raising from a daemon thread would not propagate to
                # the caller — the caller's wait loop only sees
                # ``cancelled.is_set()`` and raises a generic
                # ``CancelledError``. The real error is already logged
                # above via ``logger.exception``; that is the most we can
                # do from a background thread without changing the
                # caller's control flow.

        thread = threading.Thread(target=_monitor, daemon=True)
        thread.start()
    try:
        yield cancelled
    finally:
        cancelled.set()


def get_video_bitrate(video_path: Path) -> int | None:
    """Probe video stream bit_rate in bits/s via ffprobe (None on failure)."""
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=bit_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = run_with_retry(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            **no_window_kwargs(),
        )
        raw = result.stdout.strip()
        if not raw or raw == "N/A":
            return None
        return int(float(raw))
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
        FileNotFoundError,
        OSError,
    ) as e:
        logger.warning(f"Could not determine video bitrate for {video_path}: {e}")
        return None


def get_video_duration(video_path: Path) -> float | None:
    """Get video duration in seconds via ffprobe."""
    cmd = [
        ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = run_with_retry(
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
        OSError,
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
        ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=start_time",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = run_with_retry(
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
        OSError,
    ) as e:
        logger.warning(f"Could not determine video start_time for {video_path}: {e}")
        return 0.0


def estimate_disk_need(
    src_size: int,
    src_duration: float | None,
    keep_duration: float | None,
    method: str,
) -> tuple[int, int]:
    """Estimate typical and worst-case peak disk need for a concat run.

    Extracted from pipeline_controller so the GUI can pre-flight on Start
    click (widget reads are main-thread-only; the controller's
    estimate runs too late, after cutting already started). Returns
    (typical_bytes, worst_bytes) including a headroom buffer (20% or 512MB).
    Pure — no I/O except the caller's size/duration.
    """
    keep_ratio = 1.0
    if src_duration and src_duration > 0 and keep_duration and keep_duration > 0:
        keep_ratio = min(1.0, keep_duration / src_duration)
    keep_bytes = int(src_size * keep_ratio) if src_size else 0
    if method == "segment":
        typical = max(keep_bytes, src_size // 4)
        worst = int(keep_bytes * 2.5) if keep_bytes else int(src_size * 0.9)
        worst = max(worst, int(src_size * 0.6))
    elif method == "batch":
        typical = int(keep_bytes * 1.2) if keep_bytes else int(src_size * 0.4)
        worst = int(keep_bytes * 2.0) if keep_bytes else int(src_size * 0.7)
    else:  # cut_then_encode
        typical = int(keep_bytes * 1.1) if keep_bytes else int(src_size * 0.35)
        worst = int(keep_bytes * 1.6) if keep_bytes else int(src_size * 0.6)
    headroom = max(int(typical * 0.2), 512 * 1024 * 1024)
    return typical + headroom, worst + headroom


def check_disk_space(
    path: Path,
    required_bytes: int,
    label: str = "disk space",
) -> tuple[bool, int | None]:
    """Check free disk space at *path* against *required_bytes*.

    Returns (ok, free_bytes) — free_bytes is None when shutil.disk_usage
    fails (e.g. permission / path not mounted).
    """
    import shutil

    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        logger.warning("Could not check free space at %s: %s", path, e)
        return True, None
    return usage.free >= required_bytes, usage.free


def resolve_disk_probe(path: Path) -> Path:
    """Return the existing anchor for a disk-space probe of *path*.

    When the user names a *new* output directory (the common first-run
    case), probing that not-yet-existing path directly is wrong: the
    previous code fell back to the *source* file's parent, which sits on
    a different drive whenever the source and the destination are on
    different volumes — the check then passes against the wrong disk.
    Walk up to the nearest ancestor that does exist so
    ``shutil.disk_usage`` measures the real destination volume.
    """
    path = Path(path)
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            # Path has no anchor (relative single-component path).
            break
        probe = parent
    return probe


def has_audio_stream(video_path: Path) -> bool:
    """Return True if ``video_path`` has at least one audio stream.

    Used by the concat pipeline to decide whether to pass ``-c:a`` /
    ``-map 0:a:0`` (an audio-less source would make ffmpeg fail with
    "Output file does not contain any stream" when audio mapping is
    requested). Probed once at the start of ``cut_and_concat`` so the
    per-segment encode can skip audio options entirely for audio-less
    sources.
    """
    cmd = [
        ffprobe_path(),
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
        result = run_with_retry(
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


# Bound on stderr accumulation. A corrupt source can make ffmpeg spam
# "error while decoding MB ..." thousands of lines/sec for the whole
# stall window, and we otherwise keep it all in RAM just to truncate it
# to _STDERR_TRUNCATE chars at error time — the hoard itself can drive
# the encode's RSS over the memory-monitor budget. We keep the HEAD
# (first lines carry the encoder banner + the root error for
# ``looks_like_oom``/``classify_error``) and the TAIL (the final lines
# say what *actually* died); the middle is dropped with a marker.
_STDERR_HEAD_LINES = 200
_STDERR_TAIL_LINES = 800


def drain_stderr_lines(
    pipe: IO[str] | IO[bytes],
    sink: list[str],
    on_line: Callable[[str], None] | None = None,
) -> WaitForDrain:
    """Spawn a daemon thread that reads lines from `pipe` (bytes or text mode) and appends decoded lines to `sink`.

    The thread terminates when the pipe is closed (typically when the subprocess
    exits and the OS reaps its fds); it cannot be stopped from outside the
    thread — the only way to end it is to close the pipe.

    If `on_line` is given, it is invoked with each decoded line *after* the
    line is appended to `sink`. The callback runs on the drain thread, so
    callers that need to touch a UI must wrap the work in their framework's
    main-thread dispatch (e.g. `self.after(0, ...)` in Tkinter). Exceptions
    raised by the callback are logged and swallowed — they do not stop the
    drain thread.

    Returns a `wait_for_drain` callable that blocks for the thread to finish,
    optionally bounded by a `timeout` in seconds (`wait_for_drain()` defaults to
    30s; callers in tight watchdog loops pass a small explicit timeout). Call it
    in a `finally` block to ensure `sink` is fully populated before reading it.

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
            # Explicit EOF loop instead of iter(pipe.readline, b""): the
            # bytes sentinel never matches in text mode, where readline()
            # returns "" at EOF — the drain would spin forever (bounded
            # only by the caller's _wait_for_drain timeout) and the
            # stderr sink would stay partially filled.
            while True:
                raw = pipe.readline()
                if not raw:
                    break
                # In text mode (Popen with text=True) ``raw`` is already
                # a str; in bytes mode it's bytes. Decode bytes only so
                # text-mode callers don't trigger AttributeError.
                if isinstance(raw, bytes):
                    line = raw.decode("utf-8", errors="replace")
                else:
                    line = raw
                sink.append(line)
                # Ring trim: once past head + marker + tail, drop the
                # middle so a spammy source (corrupt input, "error while
                # decoding MB" thousands of lines/sec for hundreds of
                # seconds) can't grow the list unboundedly — it would
                # otherwise sit in RAM until the stall-kill fires and
                # eat into the very budget the memory monitor watches.
                max_lines = _STDERR_HEAD_LINES + 1 + _STDERR_TAIL_LINES
                if len(sink) > max_lines:
                    head = sink[:_STDERR_HEAD_LINES]
                    tail = sink[-_STDERR_TAIL_LINES:]
                    sink[:] = [*head, "... (middle stderr dropped)\n", *tail]
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

    def _wait_for_drain(timeout: float | None = 30.0) -> None:
        # 5s used to be the bound. On a stderr-spammy source (corrupt
        # input, "error while decoding MB" floods) the drain thread can
        # still be chewing through the pipe when the wait expires, so
        # the caller reads a PARTIAL sink and mis-classifies the run —
        # e.g. an OOM line still in the pipe becomes a generic
        # FFmpegError and the "lower the memory budget" hint is lost
        # The thread always finishes once the pipe closes
        # (process death), so 30s only stretches the bounded wait; it
        # never extends the subprocess lifetime.
        stop_event.wait(timeout=timeout)

    return _wait_for_drain


def read_lines_queue(pipe: IO[bytes]) -> tuple[queue.Queue[bytes | None], threading.Thread]:
    """Spawn a daemon thread that reads lines from ``pipe`` into a queue.

    Unlike ``drain_stderr_lines`` (which appends to a shared list and
    can't be interrupted from the consumer side), this returns a
    ``queue.Queue`` the consumer can poll with a timeout. The consumer
    loop can check cancel events / stall timeouts between reads without
    blocking on ``readline()`` — which is the stall-detection
    gap: a hung ffmpeg that stops emitting stdout blocks ``readline()``
    forever, preventing the inline stall check from running.

    The producer thread reads with ``readline()`` (blocking per-line,
    but on its own thread). When the pipe closes (subprocess exits),
    the producer puts ``None`` as a sentinel and terminates.

    Text-mode safety: the EOF test is ``not raw``, NOT ``iter(readline,
    b"")`` — the bytes sentinel never matches in text mode, where
    ``readline()`` returns ``""`` at EOF. The historical
    ``iter(pipe.readline, b"")`` loop spun forever on a text-mode pipe
    (only the daemon flag kept it from hanging the process), exactly
    the bug ``drain_stderr_lines`` guards against.

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
            # Explicit EOF loop instead of iter(pipe.readline, b""): the
            # bytes sentinel never matches in text mode (readline returns
            # "" at EOF), where the historical loop spun forever and only
            # the daemon flag kept it from hanging process exit.
            while True:
                raw = pipe.readline()
                if not raw:
                    break
                q.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    thread = threading.Thread(target=_reader, daemon=True, name="stdout_reader")
    thread.start()
    return q, thread


# Scoped process registry. The historical single-slot
# ``_active_proc`` (removed) was overwritten by every subprocess — a
# parallel preview waveform and a pipeline encode raced for the slot, and
# one's ``finally`` cleared the other's registration, so cancel/close
# couldn't reach the right process. The dict below keys processes by an
# opaque owner string (caller-chosen, e.g. "pipeline", "preview",
# "download") so multiple subprocesses can coexist and cancellation can
# target the right one.
#
# Each owner maps to a LIST so two processes registered under the SAME
# owner (two parallel previews, a preview + a pipeline) both stay
# reachable, and a context-manager exit only ever removes its OWN
# process (audit #6: the historical ``finally: set_active_process(None)``
# unconditionally wiped the slot, erasing a process B that had
# registered under the same owner before A exited).
_proc_registry: dict[str, list[subprocess.Popen]] = {}
_proc_registry_lock = threading.Lock()


def set_active_process(proc: subprocess.Popen | None, owner: str = "default") -> None:
    """Register or clear the active subprocess for ``owner``. Thread-safe.

    Passing ``proc=None`` clears ALL registrations for ``owner``
    (legacy semantic — use :func:`unregister_process` from a context
    manager so only YOUR process is removed). Multiple owners can
    coexist (e.g. "pipeline" + "preview") so parallel subprocesses don't
    clobber each other's registration.
    """
    with _proc_registry_lock:
        if proc is None:
            _proc_registry.pop(owner, None)
        else:
            _proc_registry.setdefault(owner, []).append(proc)


def unregister_process(proc: subprocess.Popen, owner: str = "default") -> None:
    """Remove exactly *proc* from ``owner``'s registrations (identity).

    Unlike ``set_active_process(None, owner)`` this never touches other
    processes registered under the same owner — audit #6: A's exit must
    not erase B's registration.
    """
    with _proc_registry_lock:
        procs = _proc_registry.get(owner)
        if not procs:
            return
        for i, p in enumerate(procs):
            if p is proc:
                del procs[i]
                break
        if not procs:
            _proc_registry.pop(owner, None)


@contextmanager
def registered_process(proc: subprocess.Popen, owner: str = "default") -> Iterator[None]:
    """Context manager that registers ``proc`` under ``owner`` on entry and
    removes exactly that process on exit (success or exception).

    Guarantees the registry slot is always cleaned by the time the block
    exits, even on cancel/timeout/error, and that the removal is scoped
    to THIS process: a concurrent run registered under the same owner
    (a second preview) survives A's exit untouched (audit #6).
    """
    set_active_process(proc, owner=owner)
    try:
        yield
    finally:
        unregister_process(proc, owner=owner)


def cancel_process(owner: str, timeout: float = 2.0) -> bool:
    """Kill ALL subprocesses registered under ``owner``. Returns True if any was killed.

    Uses ``process.kill()`` (SIGKILL on Unix, TerminateProcess on Windows)
    because we don't know which subprocess type it is and graceful
    shutdown would race with the pipeline's own cancel_callback. The
    caller's wait loop already handles the cleanup once the process exits.

    Order matters: ``kill()`` is issued *before* any pipe close. Closing
    the parent's pipe handles first raises ``ValueError: I/O operation on
    closed file`` in the drain threads still reading them (e.g. the yt-dlp
    stdout/stderr pumps in download.py); killing first lets them see EOF
    and exit cleanly.
    """
    with _proc_registry_lock:
        procs = list(_proc_registry.get(owner) or [])
    if not procs:
        return False
    killed_any = False
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.kill()
            killed_any = True
        except Exception:
            logger.exception(f"cancel_process({owner!r}): kill() failed")
            continue
        # Wait for the child to actually reap BEFORE closing
        # our pipe handles. On Windows, closing a pipe handle while a drain
        # thread is blocked in a synchronous ReadFile on it does NOT wake
        # the reader (no EBADF is delivered to the in-flight read); the
        # reader only exits when the child dies and breaks the pipe. The old
        # order closed the pipes first, so a failing ``kill()`` (or a slow
        # one) left the drain threads permanently wedged and the caller's
        # wait loop running with no data flow. ``wait(timeout)`` first, then
        # close.
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("cancel_process(%r): process did not exit within %.1fs", owner, timeout)
            # Fall through to closing the pipes anyway — if the
            # child survives (killed but wedged), the drain threads blocked in
            # ReadFile need our handle-close to see EOF and unwind. Returning
            # False here without closing would leak them until process exit.
        # Only now, after kill+wait, close OUR pipe handles. Any drain thread
        # sees EOF (the child's ends are gone) and exits.
        for pipe_name in ("stdin", "stdout", "stderr"):
            pipe = getattr(proc, pipe_name, None)
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    logger.debug(
                        "cancel_process(%r): closing %s failed", owner, pipe_name, exc_info=True
                    )
    return killed_any


def list_active_owners() -> list[str]:
    """Return the owner strings currently holding a live subprocess.

    Diagnostics-only public API (paired with ``cancel_process`` for a
    future "kill every leftover subprocess" shutdown path — see the GUI's
    ``_on_close`` which currently only kills the default owner). Not yet
    called by production code; kept because removing it would shrink the
    scoped-registry API surface the shutdown path is expected to build on.
    """
    with _proc_registry_lock:
        return [
            owner for owner, procs in _proc_registry.items() if any(p.poll() is None for p in procs)
        ]


def kill_and_reap(process: subprocess.Popen, timeout: float = 30.0) -> None:
    """Kill ``process`` and reap it with a bounded wait.

    The canonical kill-first helper for every pipeline path. On Windows
    ``kill()`` (TerminateProcess) is asynchronous; letting an exception
    escape without a ``wait()`` keeps the process handles — and any file
    the child had open (a segment ffmpeg wrote, a partial WAV) — alive
    long enough for the caller's cleanup (unlink, ``rmtree`` of a work
    dir) to trip WinError 32 (file busy). The 30s bound matches the
    historical ``_kill_and_raise`` implementations in the concat runner
    and the silence detector; a child that ignores the kill is
    un-reapable anyway. Best-effort: the kill itself may also fail
    (AccessDenied, transient handle ownership) — the caller's own error
    path must still surface.
    """
    process.kill()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass  # already dead or un-killable; nothing more to reap


def no_window_kwargs() -> dict:
    """Return subprocess kwargs that suppress console windows on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def subprocess_kwargs(low_priority: bool = False, rlimit_as_mb: int = 0) -> dict:
    """Return subprocess kwargs for ffmpeg invocations.

    Always suppresses the console window on Windows (see
    ``no_window_kwargs``). When optional flags are set, additionally:

    * ``low_priority=True``: lowers the spawned process's scheduling
      priority so a long encode doesn't starve interactive applications.
      * Windows: OR ``BELOW_NORMAL_PRIORITY_CLASS`` (0x00004000) into
        ``creationflags`` (composes with ``CREATE_NO_WINDOW``).
      * POSIX: set ``preexec_fn`` to ``os.nice(10)`` so the child
        starts at a higher nice level (lower priority).

    * ``rlimit_as_mb > 0`` (POSIX only): sets ``RLIMIT_AS`` on the
      ffmpeg child so it cannot allocate more than ``rlimit_as_mb``
      MiB of virtual address space. ``malloc`` / ``mmap`` will return
      ``ENOMEM`` (and ffmpeg will bail) before the OS swaps or the
      Linux OOM killer kicks in. This is a hard, kernel-enforced cap
      complementing the in-process ``memory_limit_mb`` pre-flight check
      (which only samples RSS *between* wall-clock polls and can miss a
      fast spike). No-op on Windows (no portable equivalent; the
      ``memory_limit_mb`` pre-flight remains the only memory door there).

    ``preexec_fn`` is unreliable in multi-threaded programs and is
    only used when one of the above is explicitly requested (opt-in
    via ``low_process_priority`` / ``rlimit_as_mb``). Default False /
    0 preserves the historical behaviour.

    When both POSIX options are requested, they are composed into a
    single ``preexec_fn`` (Python only allows one) so the child runs
    ``os.nice(10); resource.setrlimit(RLIMIT_AS, (limit, limit))``
    in that order.
    """
    kw = no_window_kwargs()
    if not low_priority and rlimit_as_mb <= 0:
        return kw
    if sys.platform == "win32":
        if low_priority:
            flags = kw.get("creationflags", 0)
            kw["creationflags"] = flags | 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
        # rlimit_as_mb is POSIX-only — ignored on Windows. The
        # in-process memory_limit_mb pre-flight remains the only
        # memory door there.
        return kw
    # POSIX: compose low_priority + rlimit_as into a single preexec_fn.
    import os
    import resource

    nice_increment = 10 if low_priority else 0
    as_bytes = int(rlimit_as_mb) * 1024 * 1024 if rlimit_as_mb > 0 else 0

    def _child_setup() -> None:
        if nice_increment:
            try:
                os.nice(nice_increment)
            except OSError:
                pass
        if as_bytes > 0:
            try:
                # RLIMIT_AS: max virtual address space bytes. malloc
                # / mmap return ENOMEM beyond this; the kernel will
                # NOT swap or trigger the hard OOM killer (this is
                # the "soft" memory door that lets ffmpeg bail
                # cleanly rather than the system swapping).
                resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
            except (OSError, ValueError):
                # An unprivileged user can tighten but not loosen;
                # setting for the first time should succeed. If the
                # system rejects the value (some BSDs reject very low
                # values that would immediately fault), silently fall
                # back to no limit — the in-process pre-flight remains
                # the safety net.
                pass

    kw["preexec_fn"] = _child_setup
    return kw


# ---------------------------------------------------------------------------
# OOM detection. ffmpeg's exit code doesn't directly say "I was
# killed by the OOM killer" — the Linux kernel sends SIGKILL (the process
# dies before it can log anything), and on Windows the exit code is 1
# (generic). The signals we *do* have:
#
#   * POSIX: returncode -9 (Python: child killed by SIGKILL) or 137
#     (shell: 128 + signal 9) — strong indicator of an OOM kill on Linux.
#   * stderr markers emitted by ffmpeg / libavcodec / libx264 before
#     they died: "out of memory", "cannot allocate memory", "malloc
#     failed", "mmap failed", "not enough space", libx264's thread
#     init failure "Error splitting input into thread: Cannot allocate
#     memory".
#
# ``looks_like_oom`` is a pure heuristic used by concat._run_ffmpeg and
# silence._run_silencedetect to surface a dedicated FFmpegOutOfMemoryError
# instead of the generic FFmpegError, so the CLI / GUI can hint the user
# to lower the memory budget or switch to the Low-memory preset.
_OOM_STDERR_MARKERS = (
    "out of memory",
    "cannot allocate memory",
    "malloc failed",
    "mmap failed",
    "not enough space",
    "error splitting input into thread",
)


def looks_like_oom(returncode: int | None, stderr_text: str) -> bool:
    """Heuristic: did ffmpeg die from an OOM condition?

    Pure / no I/O. Returns True on either signal:

    * POSIX SIGKILL (the Linux OOM killer) — ``returncode == -9``
      (Python convention: negative = killed by signal N) or
      ``returncode == 137`` (shell convention: 128 + 9).
    * stderr contains one of the canonical allocator-failure phrases
      (case-insensitive). Cross-platform — on Windows exit code is 1
      so stderr is the only signal.

    Conservative: a ``returncode == 0`` (success) with stderr text
    containing a marker returns False (the markers are warnings, not
    fatal errors, in that case). ``returncode is None`` (process still
    running) returns False.
    """
    if returncode is None or returncode == 0:
        return False
    if returncode in (-9, 137):
        return True
    if not stderr_text:
        return False
    lower = stderr_text.lower()
    return any(marker in lower for marker in _OOM_STDERR_MARKERS)
