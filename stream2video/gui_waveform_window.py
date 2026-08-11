"""WaveformMixin — popup state, open/close lifecycle, and render-size
math.

This is part of the historical ``gui_waveform.py`` single-module split.
The popup lifecycle (``_open_waveform_window``/``_on_waveform_close``)
and the size-tracking helpers live here; the zoom/pan interactions and
the render pipeline live in sibling modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import Event
from typing import Any

import customtkinter as ctk

from stream2video.gui_platform import is_previewable_input
from stream2video.silence import SilenceSegment
from stream2video.utils import cancel_process
from stream2video.waveform_view_math import compute_render_size

_logger = logging.getLogger("stream2video.gui")


class WaveformWindowMixin:
    """Waveform popup state + window lifecycle.

    Expects the host class to provide ``entry_input`` / ``entry_output``
    / ``input_var`` / ``chk_per_video_dir`` / ``config`` /
    ``_live_segments_store`` / ``_log`` / ``after*`` / ``winfo_*``.
    """

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
        """True if the waveform popup exists and is still on-screen.

        ``winfo_exists()`` can raise ``TclError("invalid command name ...")``
        when the Toplevel is mid-destroy on Windows; treat that as "not
        alive" instead of letting it propagate.
        """
        win = getattr(self, "_wave_window", None)
        if win is None:
            return False
        try:
            return bool(win.winfo_exists())
        except Exception:
            return False

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
        # Wrap winfo_exists() in try — a Toplevel caught mid-destroy on
        # Windows raises TclError("invalid command name") here, and the
        # pre-existing guard 20 lines up returns False only for the
        # already-destroyed case, not the destroying case.
        if wave_win_existing is not None:
            try:
                alive = bool(wave_win_existing.winfo_exists())
            except Exception:
                alive = False
            if alive:
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
        # ``_tk_after`` (not bare ``self.after``) so the callback is a
        # no-op when the main window is destroyed within the 50 ms window
        # — a bare ``after`` would fire ``_render_waveform_preview`` on a
        # torn-down root and raise an unhandled TclError.
        self._tk_after(50, self._render_waveform_preview)

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
        # _wave_window_alive() above guarantees self._wave_window is set;
        # narrow via a local so the .winfo_*() calls don't union-attr and
        # so ``python -O`` (which strips asserts) can't drop the guard.
        win = self._wave_window
        if win is None:
            return compute_render_size(None, None)
        return compute_render_size(win.winfo_width(), win.winfo_height())

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

    def _safe_status_set(self, text: str) -> None:
        """Update the waveform status label; no-op if the popup is closed."""
        if self.lbl_wave_status is not None:
            self.lbl_wave_status.configure(text=text)
