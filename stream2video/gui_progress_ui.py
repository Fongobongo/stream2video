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

from stream2video.gui_helpers import (
    TOTAL_ETA_MIN_PROGRESS,
    EtaSmoother,
    _wrap_status_lines,
    build_eta_tail,
    build_phase_line,
    build_progress_meta_line,
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

# Fallback phase boundaries used until the pipeline broadcasts its
# per-run ``ProgressPlan`` (``_ui_progress_plan``). Mirrors the default
# profile in pipeline_controller (PROG_DOWNLOAD_END / PROG_SILENCE_END /
# PROG_CUT_END / PROG_CONCAT_END) without importing that module into the
# GUI layer. Kept in sync with the controller's conservative defaults.
_DEFAULT_PHASE_BOUNDS: tuple[float, float, float, float] = (0.05, 0.40, 0.94, 1.0)


class ProgressUiMixin:
    """Worker → main-thread UI dispatchers for the progress widgets."""

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
        # Per-run phase boundaries (``_ui_progress_plan``) + the current
        # in-phase fraction (``_set_phase_progress``) that together drive
        # the bar's segment tick marks, the "Phase N/4" indicator, and
        # the dual percent readout.
        self._phase_bounds = _DEFAULT_PHASE_BOUNDS
        self._phase_progress: float = 0.0
        # Height of the main progress bar — the segment tick separators
        # stretch the full bar height. Kept on self so the tick layout
        # survives a widget rebuild (10 = ctk.CTkProgressBar height in
        # gui_main_window_build).
        self.phase_progress_height: int = 10

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
            self._phase_bounds = _DEFAULT_PHASE_BOUNDS
            self._phase_progress = 0.0
            self._set_progress_bar_color(None)
            self._reposition_phase_ticks()
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
            self._tk_after(0, lambda: self.lbl_progress_meta.configure(text=""))

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
        self._update_pct_label()
        self._update_progress_tooltip(
            f"Overall: {clamped * 100:.0f}% — pipeline progress; hover during a run for ETA"
        )

    def _update_pct_label(self) -> None:
        """Render the overall-percent readout next to the bar.

        The in-phase percent now lives in the status line (see
        ``_refresh_step_status``), so this label stays only the
        whole-pipeline progress — e.g. ``"42%"``.
        """
        text = f"{self._overall_progress * 100:.0f}%"
        self._tk_after(0, lambda t=text: self.lbl_progress_pct.configure(text=t))

    def _ui_progress_plan(self, bounds: tuple[float, float, float, float]) -> None:
        """Store the per-run phase boundaries broadcast by the pipeline
        and refresh the bar's segment tick marks + phase indicator."""
        self._phase_bounds = bounds
        self._reposition_phase_ticks()
        self._update_phase_indicator()

    def _reposition_phase_ticks(self) -> None:
        """Move the thin segment separators on the progress bar to the
        phase boundaries. No-op when the bar is in its pre-build state
        (the widget list only exists after ``_build_ui``)."""
        if not hasattr(self, "_phase_ticks") or not self._phase_ticks:
            return
        bounds = self._phase_bounds

        def _place() -> None:
            for i, (tick, b) in enumerate(zip(self._phase_ticks, bounds)):
                # First (download) boundary may be 0 for a local file;
                # skip it so the marker doesn't sit on the bar's left rim.
                if b <= 1e-9:
                    tick.place_forget()
                    continue
                try:
                    tick.place(
                        relx=b,
                        y=0,
                        width=1,
                        height=self.phase_progress_height,
                        bordermode="outside",
                    )
                except Exception:
                    # Don't crash a run on a layout quirk (unknown Tk).
                    pass

        self._tk_after(0, _place)

    def _update_phase_indicator(self) -> None:
        """Refresh the stage indicator status line from the current step,
        with the LIVE in-phase percent appended (e.g.
        "Step 3/4 · Cutting (63%)"). The number ticks as
        ``_set_phase_progress`` refreshes it — the status line is the
        only place the in-phase fraction is shown, so a static label
        would read as frozen. No-op when no step is running yet.
        """
        if not hasattr(self, "_current_step") or self._current_step is None:
            return
        self._set_static_status(
            build_phase_line(self._current_step, round(self._phase_progress * 100))
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
        """Record the in-phase fraction and refresh the step indicator.

        The thin per-phase bar was removed from the layout, but the
        pipeline still broadcasts its per-phase fraction via
        ``on_phase_progress`` — it now drives the LIVE percent in the
        status line ("Step 3/4 · Cutting (63%)"). The refresh is
        throttled by the same status clock as ``_ui_status`` so a busy
        phase ticks the number instead of flickering the label.
        ``phase_progress`` (the removed widget) is still guarded with a
        hasattr so a stale callback chain can't crash.
        """
        clamped = max(0.0, min(1.0, value))
        self._phase_progress = clamped
        # Always refresh the status line — regardless of whether a legacy
        # ``phase_progress`` widget attribute exists (the branch below is
        # kept only so a stale embed/test fake can't crash). Previously
        # the ``hasattr`` branch skipped the status refresh, so the live
        # percent froze on any host that still carried the attribute.
        self._update_pct_label()
        self._refresh_step_status()
        if hasattr(self, "phase_progress"):
            self._tk_after(0, lambda: self.phase_progress.set(clamped))

    def _refresh_step_status(self) -> None:
        """Re-render the step indicator with the current in-phase
        percent, throttled so phase-progress bursts don't flicker.
        No-op when no step is running yet."""
        if not hasattr(self, "_current_step") or self._current_step is None:
            return
        now = time.monotonic()
        if not should_update_status(self._last_status_update, now):
            return
        self._last_status_update = now
        self._set_static_status(
            build_phase_line(self._current_step, round(self._phase_progress * 100))
        )

    def _ui_overall(
        self,
        phase_elapsed: float,
        phase_remaining: float | None,
        more_phases: bool,
    ) -> None:
        """Update the single Elapsed/Remaining/Total readout on the bar row."""
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

        # Whole-pipeline ETA: overall_elapsed / overall_progress, gated
        # on overall progress being past the noisy-bootstrap threshold.
        overall_est: float | None = None
        if self._overall_progress >= TOTAL_ETA_MIN_PROGRESS:
            overall_est = total_elapsed / self._overall_progress
        self._tk_after(
            0,
            lambda: self.lbl_progress_meta.configure(
                text=build_progress_meta_line(total_elapsed, tail, overall_est)
            ),
        )

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
        """Update the Total wall-clock readout on the bar row (completion)."""
        self._tk_after(
            0,
            lambda: self.lbl_progress_meta.configure(
                text=build_total_line(total_elapsed, overall_est)
            ),
        )

    def _set_static_status(self, text: str) -> None:
        """Render ``text`` on the multiline status label, wrapped.

        The two wrapped lines (from ``_wrap_status_lines``) are joined
        with a newline into a single label so the row height comes only
        from the actual content — a one-line status keeps the label one
        line tall instead of reserving a fixed second row.
        """
        lines = [ln for ln in _wrap_status_lines(text) if ln]
        display = "\n".join(lines)
        self._tk_after(
            0,
            lambda t=display: self.lbl_status.configure(text=t),
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
                self._current_step = step_token
                self._phase_eta_smoother.reset()
                self._set_phase_progress(0.0)

        now = time.monotonic()
        if not should_update_status(self._last_status_update, now, force=force):
            return
        self._last_status_update = now
        if self._current_step is not None and text.startswith("Step "):
            # Status line shows the live in-phase percent
            # ("Step 3/4 · Cutting (63%)"); the timing lives in the meta
            # line and the overall percent is next to the bar. The plan
            # weight is left out — it's static per step and would read
            # as frozen progress.
            text = build_phase_line(self._current_step, round(self._phase_progress * 100))
        self._set_static_status(text)

    def _ui_info(self, text: str) -> None:
        self._tk_after(0, lambda t=text: self.lbl_silence.configure(text=t))

    def _ui_update_file_info(self, path: Path) -> None:
        self._tk_after(0, lambda: self._update_file_info(path))

    def _ui_update_output(self, path: Path) -> None:
        self._tk_after(0, lambda p=path: self.lbl_output.configure(text=f"Output: {p}"))
