"""WaveformMixin — waveform popup (preview + zoom/pan + render + poll).

Extracted from ``Stream2VideoGUI`` (Этап 10 mixin): the popup build, all
cursor / drag / wheel / slider handlers, the zoom / pan math (delegates
to ``waveform_view_math``), the render path (peaks + silence overlay),
and the live-segments poller that keeps the overlay in sync while the
pipeline runs.

State owned: ~30 ``_waveform_*`` / ``_wave_*`` attributes, initialized
in ``_init_waveform_state`` (called from the host ``__init__`` before
``_build_ui``). The cross-thread store ``_live_segments_store`` is owned
by the host (shared with the adapters).
"""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from tkinter import Event
from typing import Any

import customtkinter as ctk

from stream2video.formatters import fmt_clock_time, fmt_time, fmt_zoom_text
from stream2video.gui_platform import is_previewable_input
from stream2video.silence import (
    SilenceDetectionError,
    SilenceSegment,
    apply_margin,
    detect_silence_stream,
    load_silence_cache,
)
from stream2video.utils import cancel_process
from stream2video.waveform import (
    DB_AXIS_WIDTH,
    read_peaks_from_stream,
    render_waveform_image,
    slice_peaks_by_time,
)
from stream2video.waveform_view_math import (
    compute_pan_view,
    compute_render_size,
    compute_zoom_view,
)

_logger = logging.getLogger("stream2video.gui")


class WaveformMixin:
    """Waveform popup: preview, zoom/pan, render, and live-segments poll."""

    def _init_waveform_state(self) -> None:
        # Waveform popup state. Widgets (lbl_wave_image, lbl_wave_status)
        # are None until the user opens the popup; the render method
        # no-ops gracefully in that case.
        self._wave_window: ctk.CTkToplevel | None = None
        self.lbl_wave_image: ctk.CTkLabel | None = None
        self.lbl_wave_status: ctk.CTkLabel | None = None
        self._waveform_ctk_image: ctk.CTkImage | None = None
        self._waveform_render_token = 0
        self._waveform_running = False
        # PID of the waveform preview ffmpeg process (read_peaks_from_stream).
        # Set during preview; killed on popup close via os.kill.
        self._preview_proc_pid: int | None = None
        # Waveform view state — populated by the renderer when peaks
        # arrive, modified by the zoom/pan controls. Cleared when the
        # popup closes (see ``_on_waveform_close``).
        self._waveform_peaks: list[float] = []
        self._waveform_duration: float = 0.0
        self._waveform_margin: float = 0.0
        # Output dir the most recent waveform render resolved to, so the
        # post-pipeline poller can locate the final silence cache even
        # after the in-memory live store is dropped. ``None`` until the
        # first render runs.
        self._waveform_output_dir: Path | None = None
        self._waveform_video_name: str = ""
        self._waveform_video_path: Path | None = None
        self._waveform_view_start: float = 0.0
        self._waveform_view_end: float = 0.0
        self._waveform_cursor_frac: float = 0.5
        self._waveform_cursor_known: bool = False
        self._waveform_slider: ctk.CTkSlider | None = None
        self._waveform_zoom_label: ctk.CTkLabel | None = None
        self._waveform_tooltip: ctk.CTkLabel | None = None
        # ``after_idle`` handle for the next tooltip update. The tooltip
        # is debounced via ``after_idle`` so a fast motion across the
        # waveform doesn't leave a trail of "ghost" tooltips at the
        # previous cursor positions — place() can't always keep up at
        # motion-event rate, so we hide the tooltip during motion and
        # reshow it at the *latest* position once the event queue
        # drains. See ``_on_waveform_motion``.
        self._waveform_tooltip_after_id: str | None = None
        # Cached motion event used by the debounced tooltip update. We
        # keep the most recent event and discard older ones; the
        # callback always uses the latest event when it fires. Typed
        # as a tk Event at the class level so mypy doesn't widen the
        # first assignment (None) and flag the later Event assignment.
        self._waveform_last_motion_event: Event | None = None
        self._waveform_image_width: int = 0  # full rendered image width in px
        # Last set of margin-applied segments used to render the overlay.
        # Fallback for zoom/pan/slider re-renders when the live store
        # lookup misses (e.g., the pipeline keys _live_segments by a
        # resolved/moved path that differs from the user-input path).
        self._waveform_last_segments: list[SilenceSegment] = []
        # Left-click drag state for panning. ``_waveform_dragging``
        # gates ``_on_waveform_drag_motion`` so a single click without
        # movement is a no-op. ``_waveform_drag_press_x`` is the pixel x
        # (in the image's coordinate system) where the press landed;
        # ``_waveform_drag_view_start``/``_end`` are the view bounds at
        # press time (so the drag math is anchored, not incremental).
        self._waveform_dragging: bool = False
        self._waveform_drag_press_x: int = 0
        self._waveform_drag_view_start: float = 0.0
        self._waveform_drag_view_end: float = 0.0
        # Last popup window size we rendered for. Used to short-circuit
        # redundant <Configure> re-renders when the size didn't change.
        # ``None`` means "no render issued yet" — the first configure
        # after open should always go through.
        self._waveform_last_render_w: int | None = None
        self._waveform_last_render_h: int | None = None
        # ``after_idle`` handle for the resize-triggered re-render. Set
        # to the id returned by ``after_idle`` so we can cancel a
        # pending render when a new <Configure> fires during drag.
        self._waveform_resize_after_id: str | None = None
        # ``after`` handle for the debounced threshold-slider re-render.
        # The CTkSlider fires its ``command`` on every step of a drag, so
        # we coalesce the re-renders with a 100 ms timer: a new step
        # cancels the previous pending render. Without this the user
        # dragging the threshold slider would re-render the PIL image
        # at ~60 Hz, which is wasteful and can fall behind the cursor.
        self._waveform_threshold_after_id: str | None = None

    def _wave_window_alive(self) -> bool:
        """True if the waveform popup exists and is still on-screen."""
        win = getattr(self, "_wave_window", None)
        return win is not None and win.winfo_exists()

    def _can_preview_waveform(self) -> bool:
        """True iff the input field points at a readable local file."""
        raw = self.input_var.get().strip()
        return is_previewable_input(raw)

    def _update_waveform_button_state(self) -> None:
        """Enable / disable the Waveform button based on the current
        input. Called from the input StringVar's trace so it stays in
        sync with typing, paste, Browse, and programmatic changes."""
        btn = getattr(self, "btn_waveform", None)
        if btn is None:
            return  # button not built yet
        btn.configure(state=("normal" if self._can_preview_waveform() else "disabled"))

    def _open_waveform_window(self) -> None:
        """Open the waveform preview in a Toplevel window; auto-renders on open."""
        if not self._can_preview_waveform():
            self._log("Set a local input file before opening the waveform preview")
            return
        wave_win_existing = getattr(self, "_wave_window", None)
        if wave_win_existing is not None and wave_win_existing.winfo_exists():
            wave_win_existing.focus_force()
            wave_win_existing.lift()
            return

        win = ctk.CTkToplevel(self)
        win.title("Waveform preview")
        win.geometry("900x380")
        win.minsize(640, 300)
        self.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 900) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 380) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", lambda: self._on_waveform_close())
        win.bind("<Configure>", self._on_waveform_window_configure, add="+")

        # Status row (no render button — render fires automatically).
        status_row = ctk.CTkFrame(win, fg_color="transparent")
        status_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        status_row.grid_columnconfigure(0, weight=1)
        self.lbl_wave_status = ctk.CTkLabel(
            status_row,
            text="Opening...",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        self.lbl_wave_status.grid(row=0, column=0, sticky="ew")

        # Zoom + pan controls row.
        controls = ctk.CTkFrame(win, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 2))
        controls.grid_columnconfigure(0, weight=0)  # zoom cluster -- fixed
        controls.grid_columnconfigure(1, weight=1)  # pan cluster -- expands

        # Zoom cluster: [-] [1x] [+]
        zoom_cluster = ctk.CTkFrame(controls, fg_color="transparent")
        zoom_cluster.grid(row=0, column=0, sticky="w", padx=(0, 8))
        ctk.CTkButton(
            zoom_cluster, text="-", width=28, height=24, command=self._waveform_zoom_out
        ).pack(side="left", padx=1)
        ctk.CTkButton(
            zoom_cluster, text="1x", width=36, height=24, command=self._waveform_zoom_reset
        ).pack(side="left", padx=1)
        ctk.CTkButton(
            zoom_cluster, text="+", width=28, height=24, command=self._waveform_zoom_in
        ).pack(side="left", padx=1)
        self._waveform_zoom_label = ctk.CTkLabel(
            zoom_cluster, text="1x", width=42, anchor="w", text_color=("gray40", "gray60")
        )
        self._waveform_zoom_label.pack(side="left", padx=(6, 0))

        # Pan cluster: [<] [-----o-----] [>]
        pan_cluster = ctk.CTkFrame(controls, fg_color="transparent")
        pan_cluster.grid(row=0, column=1, sticky="ew")
        ctk.CTkButton(
            pan_cluster, text="<", width=28, height=24, command=self._waveform_pan_left
        ).pack(side="left", padx=1)
        self._waveform_slider = ctk.CTkSlider(
            pan_cluster, from_=0, to=1, height=16, command=self._on_waveform_slider
        )
        self._waveform_slider.pack(side="left", fill="x", expand=True, padx=4)
        self._waveform_slider.set(0)
        ctk.CTkButton(
            pan_cluster, text=">", width=28, height=24, command=self._waveform_pan_right
        ).pack(side="left", padx=1)

        # Image area (row 2, weight=1).
        win.grid_rowconfigure(2, weight=3)
        win.grid_columnconfigure(0, weight=1)
        self.lbl_wave_image = ctk.CTkLabel(win, text="", anchor="nw")
        self.lbl_wave_image.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 2))

        # Cut/Keep intervals list (row 3, weight=1). A small scrollable
        # textbox showing the time ranges that will be kept / cut.
        # Updated in ``_apply_view`` alongside the image render so it
        # stays in sync with the current view + segments.
        win.grid_rowconfigure(3, weight=1)
        self._waveform_intervals_text = ctk.CTkTextbox(
            win,
            height=80,
            wrap="word",
            state="disabled",
            font=("Courier", 11),
            fg_color=("gray92", "gray12"),
        )
        self._waveform_intervals_text.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        # Floating tooltip showing time + dB at the cursor over the plot.
        # Created up-front, parked with place_forget() until motion.
        self._waveform_tooltip = ctk.CTkLabel(
            win,
            text="",
            fg_color=("gray10", "gray10"),
            text_color=("white", "white"),
            corner_radius=4,
            padx=6,
            pady=2,
        )
        # Required for the label to actually render with a size we can
        # position via place(); otherwise place() uses request size
        # which is 0 before the first text update.
        self._waveform_tooltip.update_idletasks()
        # Cursor tracking for cursor-anchored zoom: x position in the
        # image maps to a time in the current view. We bind to the
        # image label so we only see motion over the waveform.
        self.lbl_wave_image.bind("<Motion>", self._on_waveform_motion)
        self.lbl_wave_image.bind("<Leave>", self._on_waveform_leave)
        # Left-click drag pans the view (like Audacity / most editors).
        # Bound only to the image label so the slider/buttons keep
        # their own behaviour.
        self.lbl_wave_image.bind("<ButtonPress-1>", self._on_waveform_drag_start)
        self.lbl_wave_image.bind("<B1-Motion>", self._on_waveform_drag_motion)
        self.lbl_wave_image.bind("<ButtonRelease-1>", self._on_waveform_drag_end)
        # Mouse wheel: zoom in/out, anchored on the cursor's last known
        # position. Bound only to the image label so it doesn't steal
        # wheel events from the slider/buttons above it.
        self.lbl_wave_image.bind("<MouseWheel>", self._on_waveform_wheel)
        self.lbl_wave_image.bind("<Button-4>", self._on_waveform_wheel)
        self.lbl_wave_image.bind("<Button-5>", self._on_waveform_wheel)

        # Stash refs so the render callback can update them.
        self._wave_window = win

        # Reset view state. The render path will populate peaks/duration
        # in Phase 1 and call _apply_view (which uses these defaults).
        self._waveform_view_start = 0.0
        self._waveform_view_end = 0.0  # set in _apply_view to match duration
        self._waveform_cursor_frac = 0.5  # 0.0-1.0 across the image, default to center
        self._waveform_cursor_known = False  # becomes True on first <Motion>

        # Auto-render. The render method no-ops gracefully if the input
        # is missing (logs an error and exits without crashing the GUI).
        self.after(50, self._render_waveform_preview)

    def _on_waveform_close(self) -> None:
        """Destroy the waveform popup and null its refs."""
        wave_win = getattr(self, "_wave_window", None)
        if wave_win is not None:
            wave_win.destroy()
        self._wave_window = None
        self.lbl_wave_status = None
        self.lbl_wave_image = None
        self._waveform_ctk_image = None
        self._waveform_slider = None
        self._waveform_zoom_label = None
        self._waveform_tooltip = None
        self._waveform_intervals_text = None
        self._waveform_image_width = 0
        self._waveform_last_render_w = None
        self._waveform_last_render_h = None
        self._waveform_resize_after_id = None
        self._waveform_threshold_after_id = None
        # Cancel any pending tooltip update so the destroyed widgets
        # don't get touched after the popup is gone.
        if getattr(self, "_waveform_tooltip_after_id", None) is not None:
            try:
                self.after_cancel(self._waveform_tooltip_after_id)
            except Exception:
                pass
            self._waveform_tooltip_after_id = None
        self._waveform_last_motion_event = None
        # Kill any ffmpeg subprocess spawned for audio peaks (Phase 1)
        # or dry-run detect (Phase 2) so it doesn't linger after popup
        # close. Uses the scoped "preview" owner (P1.11 / utils.py).
        cancel_process("preview", timeout=5.0)

    def _on_waveform_window_configure(self, event: Any) -> None:
        """Re-render the waveform when the popup is resized.

        Tk fires <Configure> for every pixel of drag-resize, so the
        callback cancels any pending render and reschedules a single
        one via ``after_idle``. ``after_idle`` coalesces by design:
        during a continuous drag the system never goes idle, so the
        re-render fires only once on release. The size-debounce in
        this handler skips bursts where the size didn't change (e.g.
        a child widget being re-laid out at the same window size).
        """
        if not self._wave_window_alive():
            return
        if event.widget is not self._wave_window:
            return
        new_w, new_h = event.width, event.height
        if new_w == self._waveform_last_render_w and new_h == self._waveform_last_render_h:
            return
        if self._waveform_resize_after_id is not None:
            try:
                self._wave_window.after_cancel(self._waveform_resize_after_id)
            except Exception:
                pass
        self._waveform_resize_after_id = self._wave_window.after_idle(self._apply_view)

    def _compute_waveform_render_size(self) -> tuple[int, int]:
        """Image size for the next render, derived from the popup window size."""
        if not self._wave_window_alive():
            return compute_render_size(None, None)
        # _wave_window_alive() above guarantees self._wave_window is set,
        # but mypy can't follow the helper's return type — narrow locally
        # so the .winfo_width/height() below don't union-attr.
        assert self._wave_window is not None
        return compute_render_size(
            self._wave_window.winfo_width(), self._wave_window.winfo_height()
        )

    def _schedule_waveform_threshold_re_render(self) -> None:
        """Schedule a debounced re-render of the waveform popup so the
        threshold line tracks the slider's current value."""
        if not self._wave_window_alive():
            return
        if getattr(self, "_waveform_threshold_after_id", None) is not None:
            try:
                self.after_cancel(self._waveform_threshold_after_id)
            except Exception:
                pass
        self._waveform_threshold_after_id = self.after(100, self._waveform_threshold_changed)

    def _waveform_threshold_changed(self) -> None:
        """Bump the render token and re-apply the view so the
        threshold line in the waveform image reflects the latest
        slider value. No-op when the popup is closed or the audio
        hasn't loaded yet.
        """
        self._waveform_threshold_after_id = None
        if not self._wave_window_alive():
            return
        if not self._waveform_peaks or self._waveform_duration <= 0:
            return
        # Cancel any in-flight render so its result doesn't overwrite
        # the freshly-computed image.
        self._waveform_render_token += 1
        self._apply_view()

    # ── Waveform cursor + zoom/pan handlers ────────────────────

    def _on_waveform_motion(self, event: Any) -> None:
        """Track the cursor's horizontal position over the image."""
        if self.lbl_wave_image is None:
            return
        try:
            width = self.lbl_wave_image.winfo_width()
        except Exception:
            return
        if width <= 0:
            return
        frac = max(0.0, min(1.0, event.x / width))
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
        self._waveform_tooltip_after_id = self.after_idle(self._show_waveform_tooltip_on_idle)

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
        plot_w = self._waveform_image_width - DB_AXIS_WIDTH
        plot_x = event.x - DB_AXIS_WIDTH
        if plot_x < 0 or plot_x >= plot_w:
            self._hide_waveform_tooltip()
            return
        view_duration = self._waveform_view_end - self._waveform_view_start
        if view_duration <= 0:
            self._hide_waveform_tooltip()
            return
        t = self._waveform_view_start + (plot_x / plot_w) * view_duration
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
        if new_start == self._waveform_view_start:
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
        if new_start == self._waveform_view_start:
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
        zoom_level = duration / view_duration if view_duration > 0 else 1.0
        if self._waveform_zoom_label is not None:
            self._waveform_zoom_label.configure(text=fmt_zoom_text(zoom_level))
        if self._waveform_slider is not None:
            self._waveform_slider.configure(to=max(duration, 1e-6))
            self._waveform_slider.set(self._waveform_view_start)

    def _render_waveform_preview(self) -> None:
        """Stream audio + silence from the source video via ffmpeg pipes
        and render the waveform with overlay. No file is written.

        Runs on a background thread so the GUI stays responsive during
        the (potentially long) first decode. Re-runs are debounced by
        ``_waveform_render_token`` — if the user clicks "Render" again
        before the previous run finishes, the older one is invalidated.

        Phase 1 streams the audio peaks directly from ffmpeg, stores
        them in self._waveform_peaks/duration, and shows the bare
        waveform for the current view (initially the full timeline).
        Phase 2 reads silence segments from the in-memory live store
        (the pipeline worker's ``on_segment`` callback keeps it up to
        date while detect is running) or, if no live state is
        available, from the final silence cache on disk. When the
        pipeline is still running, a 1-second poller keeps the overlay
        in sync with new segments as they are detected; it stops when
        ``self.running`` flips to False.

        The current view (view_start/view_end) lives in self and is
        re-rendered by the shared ``_apply_view`` helper that all
        paths (initial, poller, zoom/pan buttons, slider) call.
        """
        if self._waveform_running:
            self._log("Waveform render already running")
            return

        # Cancel any previous preview process so two renders don't
        # compete for audio decode bandwidth.
        cancel_process("preview", timeout=2.0)

        # Need an input file (must be a local file — previewing a fresh
        # download would be a separate flow). Local file → reuse it.
        input_raw = self.entry_input.get().strip()
        if not input_raw:
            self._log("Set an input video (local file) first")
            return
        in_path = Path(input_raw)
        if not in_path.is_file():
            self._log(f"Input not a local file (downloads not previewable): {input_raw}")
            return

        # Read current slider values (sync first in case FocusOut didn't fire).
        self._sync_slider_entries()
        config = {
            "threshold": float(self.config["threshold"]),
            "min_silence": float(self.config["min_silence"]),
            "margin": float(self.config["margin"]),
        }

        # Resolve the same output dir the pipeline uses — the final
        # silence cache lives there as a fallback when the in-memory
        # live store is empty (popup opened after pipeline finished).
        out_raw = self.entry_output.get().strip() or "./compressed_videos"
        out_dir = Path(out_raw).expanduser().resolve()
        if bool(self.chk_per_video_dir.get()):
            out_dir = out_dir / in_path.stem

        token = self._waveform_render_token + 1
        self._waveform_render_token = token
        self._waveform_running = True
        self._safe_status_set("Loading...")
        self._log("Waveform preview: loading audio from source video...")

        def _run() -> None:
            try:
                # Phase 1: read peaks directly from ffmpeg pipe (no WAV).
                self._tk_after(0, lambda: self._safe_status_set("Loading..."))
                peaks, duration = read_peaks_from_stream(
                    in_path,
                    target_buckets=800,
                    timeout=self.config.get("waveform_timeout", 300),
                )
                if token != self._waveform_render_token:
                    return
                if not peaks or duration <= 0:
                    self._tk_after(
                        0,
                        lambda: self._safe_status_set("No audio stream found"),
                    )
                    self._log("  Waveform preview: no audio in source")
                    return

                # Commit the audio to state.
                self._waveform_peaks = peaks
                self._waveform_duration = duration
                self._waveform_video_name = in_path.name
                self._waveform_video_path = in_path
                self._waveform_view_start = 0.0
                self._waveform_view_end = duration
                self._waveform_cursor_frac = 0.5
                self._waveform_cursor_known = False

                # Phase 1.5: render the bare waveform (no overlay yet)
                self._tk_after(
                    0,
                    lambda: self._safe_status_set("Rendering peaks... (detecting silence)"),
                )
                self._tk_after(0, lambda: self._apply_view([]))
                if token != self._waveform_render_token:
                    return

                # Phase 2: pull silence segments.
                margin = float(config["margin"])
                self._waveform_margin = margin
                self._waveform_output_dir = out_dir
                live_segs = self._take_live_snapshot(in_path)
                cached_segs = load_silence_cache(in_path, out_dir, config)
                raw_segments = live_segs if live_segs is not None else cached_segs
                if raw_segments is None:
                    cache_path = out_dir / f"{in_path.stem}_silence_cache.json"
                    # P1.16: dry-run detection.
                    self._tk_after(
                        0,
                        lambda: self._safe_status_set(
                            "No silence cache — running dry-run detect..."
                        ),
                    )
                    self._log(
                        f"  Waveform preview: no segments in live store and no cache at "
                        f"{cache_path} for threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, "
                        f"margin={config['margin']}s — running dry-run detect"
                    )
                    try:
                        raw_dry = detect_silence_stream(
                            in_path,
                            threshold=float(config["threshold"]),
                            min_silence=float(config["min_silence"]),
                        )
                    except SilenceDetectionError as e:
                        _logger.warning(f"Dry-run detect failed: {e}")
                        raw_dry = []
                    raw_segments = apply_margin(raw_dry, margin)
                    self._log(
                        f"  Dry-run detected {len(raw_segments)} silence segments "
                        f"(not cached — run the pipeline to commit)"
                    )
                # Apply margin so the overlay matches cut_and_concat.
                if live_segs is not None:
                    segments = apply_margin(raw_segments, margin)
                else:
                    segments = raw_segments
                if live_segs is not None:
                    self._log(
                        f"  Loaded {len(live_segs)} silences from live store "
                        f"(threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, margin={config['margin']}s)"
                    )
                else:
                    self._log(
                        f"  Loaded {len(cached_segs or [])} silences from final cache "
                        f"(threshold={config['threshold']}dB, "
                        f"min_silence={config['min_silence']}s, margin={config['margin']}s)"
                    )
                if token != self._waveform_render_token:
                    return

                # Phase 3: render the overlay for the current view.
                self._tk_after(0, lambda: self._safe_status_set("Rendering overlay..."))
                self._tk_after(0, lambda: self._apply_view(segments))
                if token != self._waveform_render_token:
                    return
                self._log(
                    f"  Waveform ready: {len(segments)} silence segments, "
                    f"{fmt_time(duration)} duration"
                )

                # Phase 4: if the pipeline is still running, start a
                # poller that re-renders the overlay as new segments
                # arrive in the in-memory store.
                if self.running:
                    poll_state = {
                        "last_count": len(segments),
                        "last_view": (self._waveform_view_start, self._waveform_view_end),
                    }
                    self._tk_after(
                        1000,
                        lambda: self._poll_live_segments(in_path, margin, token, poll_state),
                    )
            except Exception as e:
                _logger.exception("Waveform render failed")
                self._tk_after(0, lambda err=e: self._safe_status_set(f"Error: {err}"))
                self._log(f"[ERROR] Waveform render failed: {e}")
            finally:
                self._waveform_running = False

        threading.Thread(target=_run, daemon=True).start()

    def _take_live_snapshot(self, video_path: Path) -> list[SilenceSegment] | None:
        """Thin forward to :meth:`LiveSegmentsStore.take_snapshot`."""
        return self._live_segments_store.take_snapshot(video_path)

    def _apply_view(self, segments: list[SilenceSegment] | None = None) -> None:
        """Render the waveform for the current view (view_start → view_end)
        and apply it to the image label. No-op if the popup is closed or
        the audio hasn't been loaded yet.
        """
        if self.lbl_wave_image is None or self.lbl_wave_status is None:
            return
        if not self._waveform_peaks or self._waveform_duration <= 0:
            return

        token = self._waveform_render_token

        view_start = self._waveform_view_start
        view_end = self._waveform_view_end
        view_duration = view_end - view_start
        if view_duration <= 0 or view_duration > self._waveform_duration + 1e-6:
            view_start = 0.0
            view_end = self._waveform_duration
            view_duration = view_end - view_start
            self._waveform_view_start = view_start
            self._waveform_view_end = view_end

        view_peaks = slice_peaks_by_time(
            self._waveform_peaks, self._waveform_duration, view_start, view_end
        )

        if segments is None and self._waveform_video_path is not None:
            raw = self._take_live_snapshot(self._waveform_video_path)
            if raw is not None:
                segments = apply_margin(raw, self._waveform_margin)
            elif self._waveform_last_segments:
                segments = list(self._waveform_last_segments)
        if segments is None:
            segments = []
        self._waveform_last_segments = segments
        view_segments = [s for s in segments if s.end > view_start and s.start < view_end]

        render_w, render_h = self._compute_waveform_render_size()

        zoom_level = self._waveform_duration / view_duration
        zoom_text = fmt_zoom_text(zoom_level)
        title = (
            f"{self._waveform_video_name}  |  {len(view_segments)} silences"
            f"  |  {fmt_clock_time(view_start)}"
            f"-{fmt_clock_time(view_end)}  |  {zoom_text}"
        )

        img = render_waveform_image(
            view_peaks,
            width=render_w,
            height=render_h,
            total_duration=view_duration,
            silence_segments=view_segments,
            title=title,
            view_start=view_start,
            threshold_db=float(self.config["threshold"]),
        )

        if token != self._waveform_render_token:
            return
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self._waveform_ctk_image = ctk_img
        self.lbl_wave_image.configure(image=ctk_img, text="")
        self._waveform_image_width = img.size[0]
        self._waveform_last_render_w = img.size[0]
        self._waveform_last_render_h = img.size[1]

        self.lbl_wave_status.configure(text=title)
        self._update_waveform_controls()
        self._update_intervals_list(view_segments, segments, view_start, view_end)

    def _update_intervals_list(
        self,
        view_segments: list[SilenceSegment],
        all_segments: list[SilenceSegment],
        view_start: float,
        view_end: float,
    ) -> None:
        """Update the cut/keep intervals textbox.

        Shows a compact list of silence (cut) segments in the current
        view, with keep intervals derived between them. Format::

            CUT  0.05 - 0.25s  (0.20s)
            KEEP 0.25 - 0.35s  (0.10s)
            CUT  0.35 - 0.55s  (0.20s)
            ...

        Only the visible segments (those overlapping the current view)
        are listed — keeps the list readable when zoomed in.
        """
        widget = getattr(self, "_waveform_intervals_text", None)
        if widget is None:
            return
        if not view_segments:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "(no silence segments in view)")
            widget.configure(state="disabled")
            return

        lines: list[str] = []
        prev_end = view_start
        for seg in view_segments:
            # Keep interval before this silence (if non-trivial).
            if seg.start > prev_end + 0.01:
                keep_dur = seg.start - prev_end
                lines.append(f"  KEEP {prev_end:7.2f} - {seg.start:7.2f}s  ({keep_dur:.2f}s)")
            cut_dur = seg.end - seg.start
            lines.append(f"  CUT  {seg.start:7.2f} - {seg.end:7.2f}s  ({cut_dur:.2f}s)")
            prev_end = seg.end
        # Trailing keep after the last silence.
        if prev_end < view_end - 0.01:
            keep_dur = view_end - prev_end
            lines.append(f"  KEEP {prev_end:7.2f} - {view_end:7.2f}s  ({keep_dur:.2f}s)")

        # Header: total counts (view + all).
        header = (
            f"  {len(view_segments)} silences in view"
            f"  |  {len(all_segments)} total"
            f"  |  view {view_start:.1f}s-{view_end:.1f}s\n"
        )
        text = header + "\n".join(lines)

        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _poll_live_segments(
        self,
        in_path: Path,
        margin: float,
        token: int,
        state: dict,
    ) -> None:
        """Re-read the in-memory live store every second and re-render
        the overlay if the segment count or visible window changed."""
        if token != self._waveform_render_token:
            return
        if not self._wave_window_alive():
            return

        current_view = (self._waveform_view_start, self._waveform_view_end)
        if not self.running:
            raw = self._take_live_snapshot(in_path)
            if raw is not None:
                segments = apply_margin(raw, margin)
                if len(segments) != state["last_count"] or current_view != state["last_view"]:
                    self._apply_view(segments)
                    state["last_count"] = len(segments)
                    state["last_view"] = current_view
                    self._log(f"  Pipeline finished — waveform locked at {len(segments)} silences")
            else:
                out_dir = self._waveform_output_dir
                if out_dir is None:
                    return
                config = {
                    "threshold": float(self.config["threshold"]),
                    "min_silence": float(self.config["min_silence"]),
                    "margin": margin,
                }
                cached = load_silence_cache(in_path, out_dir, config)
                if cached is not None and len(cached) != state["last_count"]:
                    self._apply_view(list(cached))
                    state["last_count"] = len(cached)
                    state["last_view"] = current_view
                    self._log(f"  Pipeline finished — waveform locked at {len(cached)} silences")
            return

        raw = self._take_live_snapshot(in_path)
        if raw is not None:
            segments = apply_margin(raw, margin)
            count_changed = len(segments) != state["last_count"]
            view_changed = current_view != state["last_view"]
            if count_changed or view_changed:
                self._apply_view(segments)
                state["last_count"] = len(segments)
                state["last_view"] = current_view
                if count_changed:
                    self._log(f"  Waveform updated: {len(segments)} silences so far")

        self.after(
            1000,
            lambda: self._poll_live_segments(in_path, margin, token, state),
        )

    def _safe_status_set(self, text: str) -> None:
        """Update the waveform status label; no-op if the popup is closed."""
        if self.lbl_wave_status is not None:
            self.lbl_wave_status.configure(text=text)
