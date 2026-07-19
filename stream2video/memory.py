"""RAM/VRAM monitoring and OS-level guardrails.

P1.17 / Этап 8A in the fix plan. Tracks the active ffmpeg/yt-dlp
subprocess's RSS so a runaway encode (large filter graph, 4K VFR source)
can be detected and cancelled BEFORE the OS swaps itself to death —
the historical failure mode on long streams where the user's only
recourse was a hard reset.

Design:
  * ``MemoryMonitor`` is a daemon thread that polls the active
    subprocess's RSS every ``_POLL_INTERVAL`` seconds.
  * Soft threshold (default 80% of budget): warning log + set
    ``soft_exceeded`` so the pipeline can refuse to start a new
    parallel heavy task.
  * Hard threshold (default 95% of budget, OR OS reserve violated):
    cancel the current task via the same ``cancel_callback`` the user
    uses for Ctrl+C, so the existing cleanup path runs.
  * When ``psutil`` is unavailable the monitor degrades to a no-op
    with a one-time warning; the historical behaviour (no memory
    guardrail) is preserved so users without psutil aren't blocked.
  * The OS reserve is a hard floor: the monitor never lets the
    pipeline's RSS push ``available RAM`` below ``memory_reserve_mb``.
    This catches the case where the budget was computed from total RAM
    but other processes (browser, IDE) already took a chunk.

The monitor is intentionally process-scoped (one ffmpeg at a time)
rather than system-scoped — system-wide memory pressure is the OS's
job; we just keep our own footprint bounded.
"""

from __future__ import annotations

import logging
import threading
import time
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


def _process_rss_mb(pid: int) -> float | None:
    """Return the RSS of ``pid`` in MB, or None if not measurable."""
    if not _HAS_PSUTIL:
        return None
    try:
        proc = psutil.Process(pid)
        mem = proc.memory_info()
        return mem.rss / (1024 * 1024)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception:
        logger.debug(f"psutil RSS read for pid={pid} failed", exc_info=True)
        return None


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
        # ``memory_limit_mb=None`` disables the budget check; only the
        # OS reserve remains. Used when the user hasn't set a budget
        # (the default) so we still catch a runaway encode via the
        # reserve but don't enforce a soft cap.
        self.memory_limit_mb = memory_limit_mb
        self.memory_reserve_mb = memory_reserve_mb
        self.soft_threshold_frac = soft_threshold_frac
        self.hard_threshold_frac = hard_threshold_frac
        self.cancel_callback = cancel_callback
        self.on_warning = on_warning
        self.label = label

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.soft_exceeded = False
        self.hard_exceeded = False
        self.peak_rss_mb: float = 0.0

        if not _HAS_PSUTIL:
            logger.warning(
                "psutil not installed — MemoryMonitor is a no-op. "
                "Install with `pip install psutil` to enable RSS-based "
                "memory guardrails (P1.17)."
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
        while not self._stop_event.wait(_POLL_INTERVAL):
            rss = _process_rss_mb(self.pid)
            if rss is not None:
                if rss > self.peak_rss_mb:
                    self.peak_rss_mb = rss
                if self.memory_limit_mb is not None and self.memory_limit_mb > 0:
                    soft_mb = self.memory_limit_mb * self.soft_threshold_frac
                    hard_mb = self.memory_limit_mb * self.hard_threshold_frac
                    if rss >= hard_mb and not self.hard_exceeded:
                        self.hard_exceeded = True
                        msg = (
                            f"{self.label} RSS={rss:.0f}MB >= hard limit "
                            f"{hard_mb:.0f}MB ({self.hard_threshold_frac*100:.0f}% of "
                            f"{self.memory_limit_mb:.0f}MB budget) — cancelling"
                        )
                        logger.error(msg)
                        if self.on_warning is not None:
                            try:
                                self.on_warning(msg)
                            except Exception:
                                logger.debug("on_warning raised", exc_info=True)
                        if self.cancel_callback is not None:
                            try:
                                # cancel_callback returns True when the
                                # cancel took effect; the pipeline's
                                # own wait loop will reap the process.
                                self.cancel_callback()
                            except Exception:
                                logger.debug("cancel_callback raised", exc_info=True)
                        return
                    if rss >= soft_mb and not warned_soft:
                        warned_soft = True
                        self.soft_exceeded = True
                        msg = (
                            f"{self.label} RSS={rss:.0f}MB >= soft limit "
                            f"{soft_mb:.0f}MB ({self.soft_threshold_frac*100:.0f}% of "
                            f"{self.memory_limit_mb:.0f}MB budget)"
                        )
                        logger.warning(msg)
                        if self.on_warning is not None:
                            try:
                                self.on_warning(msg)
                            except Exception:
                                logger.debug("on_warning raised", exc_info=True)

            # OS reserve check — independent of the budget. Even when
            # the user hasn't set a budget, refuse to drive available
            # RAM below the reserve floor (default 2 GB).
            avail = _available_ram_mb()
            if avail is not None and avail < self.memory_reserve_mb:
                self.hard_exceeded = True
                msg = (
                    f"available RAM {avail:.0f}MB < reserve "
                    f"{self.memory_reserve_mb:.0f}MB — cancelling {self.label}"
                )
                logger.error(msg)
                if self.on_warning is not None:
                    try:
                        self.on_warning(msg)
                    except Exception:
                        logger.debug("on_warning raised", exc_info=True)
                if self.cancel_callback is not None:
                    try:
                        self.cancel_callback()
                    except Exception:
                        logger.debug("cancel_callback raised", exc_info=True)
                return


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
        return vm.total / (1024 * 1024) * 0.60
    except Exception:
        logger.debug("psutil.virtual_memory failed in auto_budget_mb", exc_info=True)
        return None


def has_psutil() -> bool:
    """True if psutil is importable — callers can branch on this."""
    return _HAS_PSUTIL


# Sleep helper for tests that want to control the timing without
# importing threading at module top (kept here to avoid circular imports).
def _sleep(seconds: float) -> None:
    time.sleep(seconds)
