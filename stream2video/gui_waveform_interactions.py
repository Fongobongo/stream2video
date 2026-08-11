"""Waveform interaction mixins: cursor tracking, tooltip, drag, zoom/pan."""

from __future__ import annotations

import logging
import math
from typing import Any

from stream2video.formatters import fmt_clock_time, fmt_zoom_text
from stream2video.waveform import DB_AXIS_WIDTH
from stream2video.waveform_view_math import (
    compute_pan_view,
    compute_zoom_view,
    cursor_plot_frac,
)

_logger = logging.getLogger("stream2video.gui")


class WaveformInteractionsMixin:
    """Cursor, tooltip, drag, wheel zoom/pan, slider, and zoom controls.

    Expects the host to provide the attributes initialized by
    :meth:`WaveformWindowMixin._init_waveform_state` and the render
    entrypoint ``_apply_view``.
    """

    # ── Cursor tracking + tooltip ──────────────────────────────────

    def _on_waveform_motion(self, event: Any) -> None:
        """Track the cursor's horizontal position over the image."""
        if self._waveform_image_width <= 0:
            return
        # Map the label-local x into a fraction of the *plot* area (the
        # dB axis strip on the left is not plot). Using the label width —
        # or ignoring the axis — mis-anchors cursor zoom and mismatches
        # the tooltip's time/dB readout; outside the plot the cursor is
        # treated as unknown so zoom falls back to the view center.
        frac = cursor_plot_frac(event.x, self._waveform_image_width, DB_AXIS_WIDTH)
        if frac is None:
            self._waveform_cursor_known = False
        else:
            self._waveform_cursor_frac = frac
            self._waveform_cursor_known = True
        self._waveform_last_motion_event = event
        if self._waveform_tooltip_after_id is not None:
            try:
                self.after_cancel(self._waveform_tooltip_after_id)
            except Exception:
                pass
            self._waveform_tooltip_after_id = None
        self._hide_waveform_tooltip()
        # ``after_idle`` raises TclError if the window is already
        # destroyed — catch it so the exception doesn't propagate to the
        # Tk event loop as an unhandled error.
        try:
            self._waveform_tooltip_after_id = self.after_idle(self._show_waveform_tooltip_on_idle)
        except Exception:
            self._waveform_tooltip_after_id = None

    def _on_waveform_leave(self, _event: Any) -> None:
        """Forget the cursor position when it leaves the image so
        subsequent zoom falls back to the view center."""
        self._waveform_cursor_known = False
        if self._waveform_tooltip_after_id is not None:
            try:
                self.after_cancel(self._waveform_tooltip_after_id)
            except Exception:
                pass
            self._waveform_tooltip_after_id = None
        self._waveform_last_motion_event = None
        self._hide_waveform_tooltip()

    def _show_waveform_tooltip_on_idle(self) -> None:
        """Fired from ``after_idle``: repaint the tooltip at the
        *latest* cached motion event. By the time this runs, the
        event queue has drained and ``place()`` will land in the
        right spot. If the popup closed or the cursor left the
        image in the meantime, the cached event is stale and we
        do nothing."""
        self._waveform_tooltip_after_id = None
        event = self._waveform_last_motion_event
        if event is None:
            return
        if self.lbl_wave_image is None:
            return
        self._update_waveform_tooltip(event)

    def _update_waveform_tooltip(self, event: Any) -> None:
        """Show a tooltip with time + dB at the cursor's plot position."""
        if (
            self._waveform_tooltip is None
            or not self._wave_window_alive()
            or not self._waveform_peaks
            or self._waveform_duration <= 0
            or self._waveform_image_width <= 0
        ):
            return
        frac = cursor_plot_frac(event.x, self._waveform_image_width, DB_AXIS_WIDTH)
        if frac is None:
            self._hide_waveform_tooltip()
            return
        view_duration = self._waveform_view_end - self._waveform_view_start
        if view_duration <= 0:
            self._hide_waveform_tooltip()
            return
        t = self._waveform_view_start + frac * view_duration
        n_peaks = len(self._waveform_peaks)
        idx = int(t / self._waveform_duration * n_peaks)
        idx = max(0, min(n_peaks - 1, idx))
        peak = self._waveform_peaks[idx]
        if peak <= 0:
            db_text = "-∞ dB"
        else:
            db = 20 * math.log10(max(peak, 1e-4))
            db_text = f"{db:+.1f} dB"
        time_text = fmt_clock_time(t)
        self._waveform_tooltip.configure(text=f"{time_text}  |  {db_text}")
        try:
            wave_win = self._wave_window
            if wave_win is None:
                return
            root_x = wave_win.winfo_rootx()
            root_y = wave_win.winfo_rooty()
        except Exception:
            return
        self._waveform_tooltip.place(
            x=event.x_root - root_x + 12,
            y=event.y_root - root_y + 12,
        )

    def _hide_waveform_tooltip(self) -> None:
        """Park the tooltip (no-op if it was never placed)."""
        if self._waveform_tooltip is None:
            return
        try:
            self._waveform_tooltip.place_forget()
        except Exception:
            pass

    # ── Zoom / pan handlers ────────────────────────────────────────

    def _on_waveform_wheel(self, event: Any) -> None:
        """Mouse wheel over the waveform: zoom by default, pan with Ctrl."""
        ctrl = bool(event.state & 0x4)  # ControlMask bit
        if event.num == 4:
            if ctrl:
                self._waveform_pan(0.25)
            else:
                self._waveform_zoom_by(0.8)
        elif event.num == 5:
            if ctrl:
                self._waveform_pan(-0.25)
            else:
                self._waveform_zoom_by(1.25)
        elif event.delta > 0:
            if ctrl:
                self._waveform_pan(0.25)
            else:
                self._waveform_zoom_by(0.8)
        elif event.delta < 0:
            if ctrl:
                self._waveform_pan(-0.25)
            else:
                self._waveform_zoom_by(1.25)

    def _on_waveform_drag_start(self, event: Any) -> None:
        """Begin a left-click drag: record the press position and the
        current view bounds so the motion handler can compute the
        new view anchored on the press (not incrementally)."""
        if self.lbl_wave_image is None or self._waveform_image_width <= 0:
            return
        if self._waveform_duration <= 0:
            return
        self._waveform_dragging = True
        self._waveform_drag_press_x = event.x
        self._waveform_drag_view_start = self._waveform_view_start
        self._waveform_drag_view_end = self._waveform_view_end

    def _on_waveform_drag_motion(self, event: Any) -> None:
        """Pan the view as the user drags. Dragging the cursor right
        shifts the visible window *earlier* in time (the content moves
        right under the cursor — like grabbing the waveform)."""
        if not self._waveform_dragging:
            return
        if self._waveform_image_width <= 0 or self._waveform_duration <= 0:
            return
        plot_w = self._waveform_image_width - DB_AXIS_WIDTH
        if plot_w <= 0:
            return
        view_duration = self._waveform_drag_view_end - self._waveform_drag_view_start
        if view_duration <= 0:
            return
        delta_x = event.x - self._waveform_drag_press_x
        new_start = self._waveform_drag_view_start - (delta_x / plot_w) * view_duration
        new_end = new_start + view_duration
        if new_start < 0:
            new_start = 0.0
            new_end = view_duration
        if new_end > self._waveform_duration:
            new_end = self._waveform_duration
            new_start = max(0.0, new_end - view_duration)
        if (new_start, new_end) == (self._waveform_view_start, self._waveform_view_end):
            return
        self._waveform_view_start = new_start
        self._waveform_view_end = new_end
        self._apply_view()

    def _on_waveform_drag_end(self, _event: Any) -> None:
        """Release the drag. A click without movement is a no-op."""
        self._waveform_dragging = False

    def _waveform_zoom_in(self) -> None:
        self._waveform_zoom_by(0.5)

    def _waveform_zoom_out(self) -> None:
        self._waveform_zoom_by(2.0)

    def _waveform_zoom_reset(self) -> None:
        """Reset to the full timeline (no zoom)."""
        duration = self._waveform_duration
        if duration <= 0:
            return
        if self._waveform_view_start == 0.0 and self._waveform_view_end == duration:
            return
        self._waveform_view_start = 0.0
        self._waveform_view_end = duration
        self._apply_view()

    def _waveform_zoom_by(self, factor: float) -> None:
        """Zoom by a multiplicative factor (< 1 = in, > 1 = out)
        anchored on the cursor's last known position (or view center
        if the cursor hasn't been over the image yet). Clamps the new
        view to [0, duration]."""
        new_start, new_end = compute_zoom_view(
            self._waveform_duration,
            self._waveform_view_start,
            self._waveform_view_end,
            self._waveform_cursor_frac,
            self._waveform_cursor_known,
            factor,
        )
        if (new_start, new_end) == (self._waveform_view_start, self._waveform_view_end):
            return
        self._waveform_view_start = new_start
        self._waveform_view_end = new_end
        self._apply_view()

    def _waveform_pan(self, frac: float) -> None:
        """Pan the view by `frac` of the current view duration
        (positive = right, negative = left). Clamps to [0, duration]."""
        new_start, new_end = compute_pan_view(
            self._waveform_duration,
            self._waveform_view_start,
            self._waveform_view_end,
            frac,
        )
        if abs(new_start - self._waveform_view_start) < 1e-9:
            return
        self._waveform_view_start = new_start
        self._waveform_view_end = new_end
        self._apply_view()

    def _waveform_pan_left(self) -> None:
        self._waveform_pan(-0.25)

    def _waveform_pan_right(self) -> None:
        self._waveform_pan(0.25)

    def _on_waveform_slider(self, value: float) -> None:
        """Slider drag: jump to the given left-edge time."""
        duration = self._waveform_duration
        view_duration = self._waveform_view_end - self._waveform_view_start
        if duration <= 0 or view_duration >= duration:
            return
        new_start = max(0.0, min(duration - view_duration, float(value)))
        # Epsilon guard, not ``==``: CTkSlider without an explicit
        # ``number_of_steps`` quantises its value onto the canvas-pixel
        # grid, so a ``set()`` followed by ``command`` fires with a
        # value that differs from what we asked for by one ULP-ish
        # amount. A strict equality guard misses that and queues a
        # redundant render loop (compute→apply→slider-update→command→
        # compute) on every drag tick.
        if abs(new_start - self._waveform_view_start) < 1e-9:
            return
        self._waveform_view_start = new_start
        self._waveform_view_end = new_start + view_duration
        self._apply_view()

    def _update_waveform_controls(self) -> None:
        """Refresh the zoom label, slider position/range, and status
        text to reflect the current view state."""
        duration = self._waveform_duration
        view_duration = self._waveform_view_end - self._waveform_view_start
        if duration <= 0 or view_duration <= 0:
            return
        zoom_level = duration / view_duration
        if self._waveform_zoom_label is not None:
            self._waveform_zoom_label.configure(text=fmt_zoom_text(zoom_level))
        if self._waveform_slider is not None:
            self._waveform_slider.configure(to=max(duration, 1e-6))
            self._waveform_slider.set(self._waveform_view_start)
