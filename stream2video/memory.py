"""RAM/VRAM monitoring and OS-level guardrails.

Tracks the active ffmpeg/yt-dlp
subprocess's RSS so a runaway encode (large filter graph, 4K VFR source)
can be detected and cancelled BEFORE the OS swaps itself to death —
the historical failure mode on long streams where the user's only
recourse was a hard reset.

Design:
  * ``MemoryMonitor`` is a daemon thread that polls the active
    subprocess's RSS every ``_POLL_INTERVAL`` seconds.
  * Soft threshold (default 80% of budget): warning log only — an
    early heads-up before the hard limit; the historical
    ``soft_exceeded`` flag was removed because no consumer ever
    materialized (the pipeline's parallel-task refusal was never built).
  * Hard threshold (default 95% of budget): cancel the current task
    via the same ``cancel_callback`` the user uses for Ctrl+C, so the
    existing cleanup path runs.
  * When ``psutil`` is unavailable the monitor degrades to a no-op
    with a one-time warning; the historical behaviour (no memory
    guardrail) is preserved so users without psutil aren't blocked.
  * The OS reserve is a WARNING floor, not a kill switch: when
    ``available RAM`` drops below ``memory_reserve_mb`` the monitor
    logs a warning once so the user sees the system is tight, but the
    encode keeps running. Cancelling a multi-minute ffmpeg on a
    transient dip (browser GC, AV scan, file cache pressure) would lose
    all of that work — and Windows recovers from memory pressure via
    standby-list trimming / paging far more gracefully than a
    cancelled encode recovers. Only the ffmpeg process's own RSS
    budget (``memory_limit_mb``) cancels the task; that one cannot
    false-fire on pressure from *other* apps the way the system-wide
    available-RAM number can.

The monitor is intentionally process-scoped (one ffmpeg at a time)
rather than system-scoped — system-wide memory pressure is the OS's
job; we just keep our own footprint bounded.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False

# Poll interval for the monitor thread. 2s is fast enough to catch a
# runaway ffmpeg before it OOMs the machine (typical RSS growth on a
# bad filter graph is ~100 MB/s) without adding noticeable overhead.
_POLL_INTERVAL = 2.0
_missing_psutil_warned = False


def _available_ram_mb() -> float | None:
    """Return currently available RAM in MB, or None if unavailable."""
    if not _HAS_PSUTIL:
        return None
    try:
        vm = psutil.virtual_memory()
        return vm.available / (1024 * 1024)
    except Exception:
        logger.debug("psutil.virtual_memory failed", exc_info=True)
        return None


def check_memory_reserve(
    memory_reserve_mb: int,
    phase_name: str,
    on_log: Callable[[str], None] | None = None,
) -> bool:
    """Return True if available RAM is above the configured reserve.

    Emits a warning when the reserve is tight (<1.5x reserve) and an
    error when the reserve is violated, then returns False so the caller
    can refuse to start the next heavy stage. ``on_log`` defaults to the
    module logger so the CLI and GUI share the same behaviour.
    """
    log = on_log if on_log is not None else lambda msg: logger.info("%s", msg)
    avail = _available_ram_mb()
    if avail is None:
        return True
    if avail < memory_reserve_mb * 1.5:
        log(
            f"[WARN] Available RAM {avail:.0f} MB is tight "
            f"(reserve={memory_reserve_mb} MB) — {phase_name} may be risky."
        )
    if avail < memory_reserve_mb:
        log(
            f"[ERROR] Available RAM {avail:.0f} MB is below reserve "
            f"{memory_reserve_mb} MB — refusing to start {phase_name}."
        )
        return False
    return True


def _process_rss_mb(pid: int) -> float | None:
    """Return the combined RSS of ``pid`` and ALL its descendants in MB,
    or None if not measurable.

    The whole process tree is summed (benchmark 2026-08 P2): on Windows
    the ffmpeg on PATH is often a package-manager SHIM (Chocolatey's
    ``ffmpeg.EXE`` wrapper) and yt-dlp runs under the venv ``python.exe``
    launcher — the pid the pipeline holds is the tiny launcher (~13 MB)
    while the real worker doing the encode is its child (measured up to
    1363 MB). Measuring only the pid under-reported RSS by two orders
    of magnitude, so the budget guard never fired and the peak-RSS log
    line reported 13 MB for a 1.3 GB encode. Descendants are collected
    recursively; one that exits mid-walk (``NoSuchProcess``) or denies
    access (``AccessDenied``) is skipped, not fatal — a partial sum is
    still closer to the truth than the launcher alone.
    """
    if not _HAS_PSUTIL:
        return None
    try:
        proc = psutil.Process(pid)
        family = [proc, *proc.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception:
        logger.debug(f"psutil process tree read for pid={pid} failed", exc_info=True)
        return None
    total = 0
    for member in family:
        try:
            total += member.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            logger.debug(f"psutil RSS read for pid={member.pid} failed", exc_info=True)
            continue
    return total / (1024 * 1024)


class MemoryMonitor:
    """Daemon thread that watches a subprocess's RSS and OS-available RAM.

    Construction does NOT start the thread — call ``start()`` once the
    subprocess has been spawned (so ``pid`` is known). The monitor
    stops automatically when the process exits OR when ``stop()`` is
    called (whichever comes first); the daemon flag also ensures it
    dies with the interpreter.

    The ``cancel_callback`` is the SAME callable the pipeline passes
    to its subprocess runners; firing it routes through the existing
    cancellation path so cleanup is consistent.
    """

    def __init__(
        self,
        pid: int,
        *,
        memory_limit_mb: float | None,
        memory_reserve_mb: float = 2048.0,
        soft_threshold_frac: float = 0.80,
        hard_threshold_frac: float = 0.95,
        cancel_callback: Callable[[], bool] | None = None,
        on_warning: Callable[[str], None] | None = None,
        label: str = "ffmpeg",
    ):
        self.pid = pid
        # ``memory_limit_mb=None`` disables the RSS budget check. The
        # OS reserve then remains as a warning-only signal — it never
        # cancels the task, so with no budget configured encode RAM is
        # effectively unbounded (as it was before).
        self.memory_limit_mb = memory_limit_mb
        self.memory_reserve_mb = memory_reserve_mb
        self.soft_threshold_frac = soft_threshold_frac
        self.hard_threshold_frac = hard_threshold_frac
        self.cancel_callback = cancel_callback
        self.on_warning = on_warning
        self.label = label

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.hard_exceeded = False
        self.peak_rss_mb: float = 0.0
        # Set when available RAM dipped below ``memory_reserve_mb`` at
        # least once. Informational only — surfaced in the final peak-RSS
        # log line; unlike ``hard_exceeded`` it does NOT cancel anything.
        self.os_reserve_breached = False

        global _missing_psutil_warned
        if not _HAS_PSUTIL and not _missing_psutil_warned:
            _missing_psutil_warned = True
            logger.warning(
                "psutil not installed — MemoryMonitor is a no-op. "
                "Install with `pip install psutil` to enable RSS-based "
                "memory guardrails."
            )

    def start(self) -> None:
        if not _HAS_PSUTIL or self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"mem_{self.label}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=_POLL_INTERVAL + 1.0)
            self._thread = None

    def _run(self) -> None:
        warned_soft = False
        warned_reserve = False
        # P2 audit: ``_process_rss_mb`` returns None when ``psutil``
        # raises ``NoSuchProcess`` (the watched ffmpeg has exited) or
        # ``AccessDenied`` (privilege boundary crossed on Windows when
        # the parent runs elevated differently from the child). Without
        # an exit guard the loop kept spinning on None forever — the
        # docstring promises "stops automatically when the process
        # exits", but the loop only slept and re-checked. Track
        # consecutive None readings and exit after 2 in a row so a
        # single transient None (AccessDenied during a brief permission
        # probe) doesn't abort the monitor, but a genuinely gone PID
        # releases the thread within ``2*_POLL_INTERVAL``.
        consecutive_none = 0
        while not self._stop_event.wait(_POLL_INTERVAL):
            rss = _process_rss_mb(self.pid)
            if rss is None:
                consecutive_none += 1
                if consecutive_none >= 2:
                    logger.debug(
                        "MemoryMonitor: pid=%s unavailable for %d consecutive "
                        "polls (process exited or access denied) — stopping monitor",
                        self.pid,
                        consecutive_none,
                    )
                    return
                continue
            consecutive_none = 0
            if rss > self.peak_rss_mb:
                self.peak_rss_mb = rss
            # Budget check runs on EVERY poll, not only when a new peak
            # is observed: an encode that plateaus right at the limit
            # (RSS stays flat above the soft/hard threshold) would
            # otherwise never be flagged, because the old nesting inside
            # ``if rss > peak`` stopped checking once the peak was set.
            if self.memory_limit_mb is not None and self.memory_limit_mb > 0:
                soft_mb = self.memory_limit_mb * self.soft_threshold_frac
                hard_mb = self.memory_limit_mb * self.hard_threshold_frac
                if rss >= hard_mb and not self.hard_exceeded:
                    msg = (
                        f"{self.label} RSS={rss:.0f}MB >= hard limit "
                        f"{hard_mb:.0f}MB ({self.hard_threshold_frac * 100:.0f}% of "
                        f"{self.memory_limit_mb:.0f}MB budget) — cancelling"
                    )
                    logger.error(msg)
                    # Set the flag BEFORE the callbacks so the pipeline's
                    # wait loop can distinguish an OOM-triggered cancel
                    # from a user cancel as early as possible. The old
                    # order set it only after ``cancel_callback`` ran, so
                    # a cancel taking effect quickly (process killed,
                    # runner sees rc) could be observed by the pipeline
                    # while ``hard_exceeded`` was still False — the kill
                    # was then misreported as a user CancelledError
                    # instead of FFmpegOutOfMemoryError, losing the
                    # "lower the memory budget" hint.
                    self.hard_exceeded = True
                    if self.on_warning is not None:
                        try:
                            self.on_warning(msg)
                        except Exception:
                            logger.debug("on_warning raised", exc_info=True)
                    if self.cancel_callback is not None:
                        try:
                            # cancel_callback is best-effort: the
                            # pipeline's own wait loop will reap the
                            # process and inspect ``hard_exceeded`` to
                            # pick the OOM error path.
                            self.cancel_callback()
                        except Exception:
                            logger.debug("cancel_callback raised", exc_info=True)
                    return
                if rss >= soft_mb and not warned_soft:
                    warned_soft = True
                    msg = (
                        f"{self.label} RSS={rss:.0f}MB >= soft limit "
                        f"{soft_mb:.0f}MB ({self.soft_threshold_frac * 100:.0f}% of "
                        f"{self.memory_limit_mb:.0f}MB budget)"
                    )
                    logger.warning(msg)
                    if self.on_warning is not None:
                        try:
                            self.on_warning(msg)
                        except Exception:
                            logger.debug("on_warning raised", exc_info=True)

            # OS reserve check — warning only, never cancels. The
            # available-RAM reading reflects pressure from ALL processes
            # (browser, IDE, file cache churn), so cancelling a running
            # encode on it trades a guaranteed loss of the work already
            # done for a hypothetical swap the OS usually handles by
            # trimming the standby list. Surface it once so the user
            # can see the system was tight; the per-process RSS budget
            # above remains the only hard door.
            avail = _available_ram_mb()
            if avail is not None and avail < self.memory_reserve_mb:
                self.os_reserve_breached = True
                if not warned_reserve:
                    warned_reserve = True
                    msg = (
                        f"available RAM {avail:.0f}MB < reserve "
                        f"{self.memory_reserve_mb:.0f}MB — system memory "
                        f"is tight; {self.label} continues (reserve "
                        "violation is logged, not enforced; see "
                        "memory_reserve_mb docs)"
                    )
                    logger.warning(msg)
                    if self.on_warning is not None:
                        try:
                            self.on_warning(msg)
                        except Exception:
                            logger.debug("on_warning raised", exc_info=True)


def auto_budget_mb() -> float | None:
    """Compute a default RAM budget = 60% of total RAM, or None if unknown.

    Used when the user sets ``memory_limit_mb='auto'`` (the default in
    ``CONFIG_DEFAULTS``). 60% leaves room for the OS, browser, IDE, and
    any other tools the user keeps open while a long encode runs. The
    remaining 40% is also the source of the default ``memory_reserve_mb``
    floor — if the user has 16 GB, the budget is 9.6 GB and the reserve
    is 2 GB, so the pipeline is capped well before the system swaps.
    """
    if not _HAS_PSUTIL:
        return None
    try:
        vm = psutil.virtual_memory()
        budget = vm.total / (1024 * 1024) * 0.60
        # On very low-RAM systems (512 MB total → ~307 MB budget) the
        # monitor would kill almost any encode immediately. Impose a
        # floor so the pipeline remains usable; the reserve floor still
        # protects the OS.
        return max(budget, 512.0)
    except Exception:
        logger.debug("psutil.virtual_memory failed in auto_budget_mb", exc_info=True)
        return None
