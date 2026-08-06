"""ProgressUiMixin — progress bar / status line / cancel button (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: the ``_ui_*`` family (worker-
thread → main-thread dispatchers for every progress widget), the
running/cancel state, and the button-state toggle.

State owned: ``running``, ``_cancel_event``, ``_last_status_update``,
``_pipeline_start``, ``_output_path``, ``_download_path``. The cross-
thread dispatcher (``self._tk_after``) and ``self._log`` stay on the
host GUI class because they're shared with every other mixin.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import ClassVar

from stream2video.gui_helpers import (
    STATUS_MAX,
    TOTAL_ETA_MIN_PROGRESS,
    EtaSmoother,
    _wrap_status_lines,
    build_eta_tail,
    build_overall_line,
    build_total_line,
    should_update_status,
)

# Refresh period of the throttled Overall line + total-ETA (seconds).
# The phases emit on_progress/on_overall bursts (multiple updates per
# second during a fast phase); redrawing the labels at that rate would
# just flicker — once a second is enough for a wall-clock readout.
OVERALL_UPDATE_INTERVAL = 1.0

# Progress-bar colours for the pipeline outcome (point 6 of the
# progress-UI improvements). The running bar keeps the theme accent.
_BAR_SUCCESS = "#2e7d32"
_BAR_FAILURE = "#d32f2f"

# Progress-bar colour used when the bar holds partial-progress after a
# failure/cancel — visually distinct from the "live" running bar and the
# final all-green success bar.
_PCT_FAILURE_COLOR = ("gray50", "gray50")


class ProgressUiMixin:
    """Worker → main-thread UI dispatchers for the progress widgets."""

    _STATUS_MAX: ClassVar[int] = STATUS_MAX

    def _init_progress_ui(self) -> None:
        # Per-run state the worker thread reads through the adapter.
        # Owned here because it's tightly coupled with the ``_ui_*``
        # methods below (``_pipeline_start`` is set by ``_pipeline_worker``
        # and read by ``_ui_overall``; ``running`` is set by
        # ``_set_running`` and read by WaveformMixin's poller).
        self.running: bool = False
        self._cancel_event: threading.Event = threading.Event()
        self._last_status_update: float = 0.0
        self._pipeline_start: float | None = None
        self._output_path: Path | None = None
        self._download_path: Path | None = None
        # ETA smoother + overall-progress snapshot used by ``_ui_overall``
        # to render the whole-pipeline ETA (``Total: X / ~Y``). Smoothed
        # so the readout doesn't jitter second-to-second (P1.1/P1.2 of
        # the progress-UI improvement plan).
        self._phase_eta_smoother = EtaSmoother()
        self._overall_progress: float = 0.0
        # Theme default cached at widget-build time (``_build_ui``) so a
        # success/failure tint can be restored on the next run. Optional
        # tuple type holds ``str`` colours too (customtkinter returns a
        # tuple for themed values, a plain str once overridden).
        self._default_progress_color: str | tuple | None = None
        self._last_overall_update: float = 0.0
        # Tracks which "Step X/4" status line we're on so the ETA
        # smoother + thin per-phase bar reset at each phase boundary
        # (``_ui_status`` parses the prefix).
        self._current_step: str | None = None

    def _cancel_pipeline(self) -> None:
        if self.running:
            self._cancel_event.set()
            self._log("Cancelling... (will stop after current step)")

    def _set_running(self, state: bool) -> None:
        self.running = state
        if state:
            self.btn_start.configure(state="disabled", text="Running...")
            self.btn_cancel.configure(state="normal")
            self._phase_eta_smoother.reset()
            self._current_step = None
            self._overall_progress = 0.0
            self._set_progress_bar_color(None)
            self._tk_after(
                0,
                lambda: self.lbl_progress_pct.configure(
                    text_color=("gray40", "gray60")
                ),
            )
            self._set_phase_progress(0.0)
        else:
            self.btn_start.configure(state="normal", text="Start")
            self.btn_cancel.configure(state="disabled")
            # Clear the time labels so a stale state is not shown on the
            # next pipeline's idle state.
            self._pipeline_start = None
            self._tk_after(0, lambda: self.lbl_overall.configure(text=""))
            self._tk_after(0, lambda: self.lbl_total.configure(text=""))

    def _set_progress_bar_color(self, color: str | None) -> None:
        """Recolor the main progress bar; ``None`` restores the default.

        Worker-thread safe: dispatches the ``configure`` to the Tk main
        loop. Green on success / red on failure; the bar value itself is
        left untouched so a partial failure still shows how far it got.
        """
        if color is None:
            if self._default_progress_color is None:
                return
            restore: object = self._default_progress_color
            self._tk_after(
                0,
                lambda c=restore: self.progress.configure(progress_color=c),
            )
            return
        self._tk_after(0, lambda c=color: self.progress.configure(progress_color=c))

    def _ui_progress(self, value: float) -> None:
        clamped = max(0.0, min(1.0, value))
        self._overall_progress = clamped
        self._tk_after(0, lambda: self.progress.set(clamped))
        self._tk_after(0, lambda: self.lbl_progress_pct.configure(text=f"{clamped * 100:.0f}%"))
        self._update_progress_tooltip(
            f"Overall: {clamped * 100:.0f}% — pipeline progress; hover during a run for ETA"
        )

    def _ui_set_failure_style(self) -> None:
        """Paint the progress bar + percent label with the failure colours.

        Called on Pipeline*Error / cancel. Leaves the bar value alone —
        the user sees how far the pipeline got before the failure.
        """
        self._set_progress_bar_color(_BAR_FAILURE)
        self._tk_after(
            0,
            lambda: self.lbl_progress_pct.configure(text_color=_PCT_FAILURE_COLOR),
        )

    def _ui_set_success_style(self) -> None:
        """Green bar on pipeline completion (``on_pipeline_complete``)."""
        self._set_progress_bar_color(_BAR_SUCCESS)

    def _set_phase_progress(self, value: float) -> None:
        """Update the thin per-phase bar (point 3 of the improvement plan).

        ``value`` is a fraction within the CURRENT phase (0..1) — the
        segment inside the phase's span of the overall bar. Called from
        the phase-progress callbacks; no-op when the widget isn't built
        yet (tests, partial init).
        """
        if not hasattr(self, "phase_progress"):
            return
        clamped = max(0.0, min(1.0, value))
        self._tk_after(0, lambda: self.phase_progress.set(clamped))

    def _ui_overall(
        self,
        phase_elapsed: float,
        phase_remaining: float | None,
        more_phases: bool,
    ) -> None:
        """Update the live Elapsed/Remaining line + the Total wall-clock label."""
        if self._pipeline_start is None:
            return
        now = time.monotonic()
        if not should_update_status(
            self._last_overall_update, now, interval=OVERALL_UPDATE_INTERVAL
        ):
            return
        self._last_overall_update = now
        total_elapsed = now - self._pipeline_start

        # Smoothed per-phase ETA (kills the second-to-second jitter).
        smoothed_phase = self._phase_eta_smoother.update(phase_remaining)
        tail = build_eta_tail(smoothed_phase, more_phases)
        self._tk_after(0, lambda: self.lbl_overall.configure(text=build_overall_line(total_elapsed, tail)))

        # Whole-pipeline ETA: overall_elapsed / overall_progress, gated
        # on overall progress being past the noisy-bootstrap threshold.
        overall_est: float | None = None
        if self._overall_progress >= TOTAL_ETA_MIN_PROGRESS:
            overall_est = total_elapsed / self._overall_progress
        self._ui_total(total_elapsed, overall_est=overall_est)

        # Live tooltip on the bar with the raw + smoothed numbers.
        tip = (
            f"Overall: {self._overall_progress * 100:.0f}% | "
            f"Phase ETA: {tail} | Total: {build_total_line(total_elapsed, overall_est)[7:]}"
        )
        self._update_progress_tooltip(tip)

    def _update_progress_tooltip(self, text: str) -> None:
        """Refresh the hover tooltip on the progress bar. No-op when the
        widget (or its tooltip) hasn't been built yet."""
        if hasattr(self, "progress_tooltip"):
            self.progress_tooltip.text = text

    def _ui_total(self, total_elapsed: float, *, overall_est: float | None = None) -> None:
        """Update the Total wall-clock label below the progress bar."""
        self._tk_after(
            0,
            lambda: self.lbl_total.configure(text=build_total_line(total_elapsed, overall_est)),
        )

    def _ui_status(self, text: str, force: bool = False) -> None:
        # Phase-switch detection runs unconditionally (NOT throttled):
        # even when the status-line redraw is rate-limited, dropping the
        # reset of the ETA smoother + the per-phase bar would leak the
        # previous phase's ETA into the new one.
        import re as _re

        if text.startswith("Step "):
            parts = text.split(":", 1)[0].split("/")
            raw = parts[0][5:] if len(parts) > 0 and len(parts[0]) > 5 else ""
            m = _re.match(r"(\d+)", raw)
            step_token = m.group(1) if m else None
            if step_token is not None and step_token != self._current_step:
                self._current_step = step_token  # type: ignore[assignment]
                self._phase_eta_smoother.reset()
                self._set_phase_progress(0.0)

        now = time.monotonic()
        if not should_update_status(self._last_status_update, now, force=force):
            return
        self._last_status_update = now
        lines = _wrap_status_lines(text)
        line1 = lines[0] if len(lines) > 0 else ""
        line2 = lines[1] if len(lines) > 1 else ""
        self._tk_after(0, lambda t1=line1, t2=line2: (self.lbl_status.configure(text=t1), getattr(self, "lbl_status2", None) and self.lbl_status2.configure(text=t2)))

    def _ui_info(self, text: str) -> None:
        self._tk_after(0, lambda t=text: self.lbl_silence.configure(text=t))

    def _ui_update_file_info(self, path: Path) -> None:
        self._tk_after(0, lambda: self._update_file_info(path))

    def _ui_update_output(self, path: Path) -> None:
        self._tk_after(0, lambda p=path: self.lbl_output.configure(text=f"Output: {p}"))
