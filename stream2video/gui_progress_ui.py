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

from stream2video.formatters import fmt_total_label
from stream2video.gui_helpers import (
    STATUS_MAX,
    build_eta_tail,
    build_overall_line,
    should_update_status,
    truncate_status,
)


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

    def _cancel_pipeline(self) -> None:
        if self.running:
            self._cancel_event.set()
            self._log("Cancelling... (will stop after current step)")

    def _set_running(self, state: bool) -> None:
        self.running = state
        if state:
            self.btn_start.configure(state="disabled", text="Running...")
            self.btn_cancel.configure(state="normal")
        else:
            self.btn_start.configure(state="normal", text="Start")
            self.btn_cancel.configure(state="disabled")
            # Clear the time labels so a stale state is not shown on the
            # next pipeline's idle state.
            self._pipeline_start = None
            self._tk_after(0, lambda: self.lbl_overall.configure(text=""))
            self._tk_after(0, lambda: self.lbl_total.configure(text=""))

    def _ui_progress(self, value: float) -> None:
        self._tk_after(0, lambda: self.progress.set(max(0.0, min(1.0, value))))

    def _ui_overall(
        self,
        phase_elapsed: float,
        phase_remaining: float | None,
        more_phases: bool,
    ) -> None:
        """Update the live Elapsed/Remaining line + the Total wall-clock label."""
        if self._pipeline_start is None:
            return
        total_elapsed = time.monotonic() - self._pipeline_start
        tail = build_eta_tail(phase_remaining, more_phases)
        text = build_overall_line(total_elapsed, tail)
        self._tk_after(0, lambda: self.lbl_overall.configure(text=text))
        self._ui_total(total_elapsed)

    def _ui_total(self, total_elapsed: float) -> None:
        """Update the Total wall-clock label below the progress bar."""
        self._tk_after(
            0,
            lambda: self.lbl_total.configure(text=fmt_total_label(total_elapsed)),
        )

    def _ui_status(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not should_update_status(self._last_status_update, now, force=force):
            return
        self._last_status_update = now
        text = truncate_status(text, self._STATUS_MAX)
        self._tk_after(0, lambda: self.lbl_status.configure(text=text))

    def _ui_info(self, text: str) -> None:
        self._tk_after(0, lambda t=text: self.lbl_silence.configure(text=t))

    def _ui_update_file_info(self, path: Path) -> None:
        self._tk_after(0, lambda: self._update_file_info(path))

    def _ui_update_output(self, path: Path) -> None:
        self._tk_after(0, lambda p=path: self.lbl_output.configure(text=f"Output: {p}"))
