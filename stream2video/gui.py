"""stream2video GUI — cross-platform desktop application."""

import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import ClassVar

import customtkinter as ctk
from PIL import Image

from stream2video.concat import (
    CancelledError,
    ConcatError,
    check_encoder,
    cut_and_concat,
    generate_keep_segments,
)
from stream2video.config import (
    CONFIG_DEFAULTS,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_THEMES,
    coerce_typed_value,
    effective_defaults,
    save_user_defaults,
    user_defaults_path,
)
from stream2video.download import DownloadCancelledError, DownloadError, download
from stream2video.paths import (
    add_recent_project,
    ensure_project_dir,
    move_into_project,
    prune_recent_projects,
)
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)
from stream2video.utils import get_active_process, get_video_duration
from stream2video.waveform import (
    read_waveform_peaks,
    render_waveform_image,
)

logger = logging.getLogger("stream2video.gui")


# Max length of the displayed name in a Recent Projects row. Long
# filenames (e.g. "<id>_compressed_4_30_<more>") are truncated with
# an ellipsis so the column doesn't grow to fit the longest name.
# The full name is still available on hover via the tooltip.
_RECENT_NAME_MAX = 24


def _truncate(text: str, max_len: int) -> str:
    """Truncate ``text`` to ``max_len`` chars, appending an ellipsis if cut.

    If ``text`` is shorter than or equal to ``max_len``, it is returned
    unchanged. Otherwise the first ``max_len - 1`` characters are kept
    and "…" is appended (so the result is exactly ``max_len`` chars).
    """
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


class _Tooltip:
    """A hover tooltip for any tkinter/ctk widget."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self._tip: ctk.CTkToplevel | None = None
        self._after_id: str | None = None
        widget.bind("<Enter>", self._schedule_show, add="+")
        widget.bind("<Leave>", self._schedule_hide, add="+")

    def _schedule_show(self, event=None):
        self._cancel_scheduled()
        self._after_id = self.widget.after(400, self._show)

    def _schedule_hide(self, event=None):
        self._cancel_scheduled()
        if self._tip:
            self.widget.after(200, self._hide)

    def _cancel_scheduled(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self, event=None):
        self._after_id = None
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        ctk.CTkLabel(
            tw,
            text=self.text,
            wraplength=320,
            fg_color=("gray85", "gray15"),
            text_color=("gray10", "gray90"),
            corner_radius=4,
            padx=8,
            pady=4,
        ).pack()
        tw.bind("<Enter>", self._cancel_scheduled, add="+")
        tw.bind("<Leave>", self._schedule_hide, add="+")

    def _hide(self, event=None):
        tw = self._tip
        self._tip = None
        if tw:
            tw.destroy()


class QueueHandler(logging.Handler):
    """Send log records to a queue for GUI display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


def _build_completion_summary(
    src_size_bytes: int,
    src_duration: float | None,
    dst_size_bytes: int,
    dst_duration: float,
    pipeline_seconds: float,
    output_path: str,
) -> dict:
    """Build the user-facing strings emitted on pipeline completion.
    Pure function — no Tk / no side effects — so it can be unit-tested
    without instantiating the GUI.

    Returns a dict with keys:
      - status:    one-line headline for the status bar: 'Complete!' plus
                   the total wall-clock in parentheses, e.g. 'Complete! (23m 5s)'.
                   Size and duration go in the log block and the popup only.
      - log_lines: list of log lines (with '=' separators) for grep-ability.
                   Contains the full src->dst size/duration breakdown.
      - popup:     multi-line message for the 'Complete' messagebox.
                   Full breakdown with Source/Output labels.
    """
    src_size_s = Stream2VideoGUI._fmt_size(src_size_bytes)
    dst_size_s = Stream2VideoGUI._fmt_size(dst_size_bytes)
    src_dur_s = Stream2VideoGUI._fmt_clock_time(src_duration)
    dst_dur_s = Stream2VideoGUI._fmt_clock_time(dst_duration)
    pipe_s = Stream2VideoGUI._fmt_time(pipeline_seconds)

    sep = "=" * 60
    log_lines = [
        sep,
        f"[SUCCESS] Output: {output_path}",
        f"  Size:     {src_size_s} -> {dst_size_s}",
        f"  Duration: {src_dur_s} -> {dst_dur_s}",
        f"  Pipeline: {pipe_s}",
        sep,
    ]

    popup = (
        f"Video saved to:\n{output_path}\n\n"
        f"Source:  {src_size_s}, {src_dur_s}\n"
        f"Output:  {dst_size_s}, {dst_dur_s}\n\n"
        f"Pipeline: {pipe_s}"
    )

    return {
        "status": f"Complete! ({pipe_s})",
        "log_lines": log_lines,
        "popup": popup,
    }


class Stream2VideoGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("stream2video")

        self.running = False
        self._cancel_event = threading.Event()
        self._test_running = False
        self.config = effective_defaults()
        self.log_queue: queue.Queue = queue.Queue()
        self._output_path: Path | None = None
        self._download_path: Path | None = None
        self._last_status_update: float = 0.0
        self._pipeline_start: float | None = None

        self._load_settings()
        ctk.set_appearance_mode(self.config["theme"])

        # Fit window to screen if resolution is small
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = max(1, min(1080, sw - 40))
        win_h = max(1, min(680, sh - 60))
        self.minsize(max(1, min(1000, sw - 40)), max(1, min(620, sh - 60)))

        geom = self.config.get("window_geometry")
        if geom:
            try:
                gw = int(geom.split("x")[0])
                gh = int(geom.split("x")[1].split("+")[0])
                if gw <= sw and gh <= sh:
                    self.geometry(geom)
                else:
                    self.geometry(f"{win_w}x{win_h}")
            except Exception:
                self.geometry(f"{win_w}x{win_h}")
        else:
            self.geometry(f"{win_w}x{win_h}")

        self._build_ui()
        self._setup_logging()
        self.after(100, self._poll_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Build ──────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=210)
        self.grid_columnconfigure(1, weight=1, minsize=360)
        self.grid_columnconfigure(2, weight=1, minsize=360)
        self.grid_rowconfigure(0, weight=1)

        # ── Left: Info Panel ──
        info_frame = ctk.CTkFrame(self)
        info_header = ctk.CTkLabel(
            info_frame, text="Info", anchor="w", font=ctk.CTkFont(size=12, weight="bold")
        )
        info_header.pack(fill="x", padx=5, pady=(4, 2))
        info_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 3), pady=4)

        self.lbl_file = ctk.CTkLabel(info_frame, text="File: —", wraplength=190, justify="left")
        self.lbl_file.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_output = ctk.CTkLabel(info_frame, text="Output: —", wraplength=190, justify="left")
        self.lbl_output.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_size = ctk.CTkLabel(info_frame, text="Size: —", wraplength=190, justify="left")
        self.lbl_size.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_duration = ctk.CTkLabel(
            info_frame, text="Duration: —", wraplength=190, justify="left"
        )
        self.lbl_duration.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_silence = ctk.CTkLabel(
            info_frame, text="Silence: —", wraplength=190, justify="left"
        )
        self.lbl_silence.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_encoder = ctk.CTkLabel(
            info_frame, text="Encoder: —", wraplength=190, justify="left"
        )
        self.lbl_encoder.pack(anchor="w", fill="x", padx=5, pady=1)

        ctk.CTkFrame(info_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=6
        )

        # Recent Projects section
        ctk.CTkLabel(info_frame, text="Recent Projects", anchor="w").pack(
            fill="x", padx=5, pady=(0, 2)
        )
        self.recent_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
        self.recent_frame.pack(fill="x", padx=2, pady=(0, 4))
        self._render_recent_projects()

        ctk.CTkFrame(info_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=(6, 4)
        )

        ctk.CTkLabel(info_frame, text="Theme:", anchor="w").pack(fill="x", padx=5, pady=(0, 1))
        self.combo_theme = ctk.CTkComboBox(
            info_frame, values=VALID_THEMES, state="readonly", command=self._on_theme_change
        )
        self.combo_theme.set(self.config["theme"])
        self.combo_theme.pack(fill="x", padx=5, pady=(0, 4))

        ctk.CTkButton(
            info_frame, text="Save current as defaults", command=self._save_user_defaults
        ).pack(fill="x", padx=5, pady=(0, 4))
        ctk.CTkButton(info_frame, text="Restore defaults", command=self._restore_defaults).pack(
            fill="x", padx=5, pady=(0, 4)
        )
        ctk.CTkButton(info_frame, text="Copy CLI command", command=self._copy_cli_command).pack(
            fill="x", padx=5, pady=(0, 4)
        )

        # ── Center: Controls ──
        ctrl_frame = ctk.CTkFrame(self)
        ctrl_frame.grid(row=0, column=1, sticky="nsew", padx=3, pady=4)

        ctk.CTkLabel(
            ctrl_frame, text="Controls", anchor="w", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(fill="x", padx=5, pady=(6, 2))
        # Input
        ctk.CTkLabel(
            ctrl_frame, text="Input Video (Twitch/YouTube URL or local path):", anchor="w"
        ).pack(fill="x", padx=5, pady=(1, 1))
        row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row.pack(fill="x", padx=5, pady=(0, 5))
        self.entry_input = ctk.CTkEntry(row, placeholder_text="video.mp4 or https://...")
        self.entry_input.pack(side="left", fill="x", expand=True)
        if self.config.get("input_path"):
            self.entry_input.insert(0, self.config["input_path"])
        ctk.CTkButton(row, text="Browse", width=70, command=self._browse_input).pack(
            side="right", padx=(5, 0)
        )

        # Output dir
        ctk.CTkLabel(ctrl_frame, text="Output Directory:", anchor="w").pack(
            fill="x", padx=5, pady=(0, 1)
        )
        row2 = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row2.pack(fill="x", padx=5, pady=(0, 6))
        self.entry_output = ctk.CTkEntry(row2, placeholder_text="compressed_videos")
        self.entry_output.pack(side="left", fill="x", expand=True)
        if self.config.get("output_dir"):
            self.entry_output.insert(0, self.config["output_dir"])
        ctk.CTkButton(row2, text="Browse", width=70, command=self._browse_output).pack(
            side="right", padx=(5, 0)
        )

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=3
        )

        # Config
        ctk.CTkLabel(ctrl_frame, text="Silence Detection", anchor="w", font=("", 13, "bold")).pack(
            fill="x", padx=5, pady=(3, 1)
        )

        self._add_slider(
            ctrl_frame,
            "Threshold (dB):",
            "threshold",
            -60,
            -5,
            self.config["threshold"],
            tooltip="Audio below this level is considered silence. Lower (-30) removes more noise, higher (-5) only cuts loud pauses.",
        )
        self._add_slider(
            ctrl_frame,
            "Min Silence (s):",
            "min_silence",
            0.1,
            60,
            self.config["min_silence"],
            tooltip="Minimum silence duration to cut (seconds). Longer values prevent choppy edits.",
        )
        self._add_slider(
            ctrl_frame,
            "Margin (s):",
            "margin",
            -3,
            5,
            self.config["margin"],
            tooltip="How much to shrink silence zones. Positive = shrink silence (keep more audio around phrases). Negative = expand silence (cut more aggressively). 0 = no adjustment.",
        )

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=3
        )

        # Options
        ctk.CTkLabel(ctrl_frame, text="Options", anchor="w", font=("", 13, "bold")).pack(
            fill="x", padx=5, pady=(3, 1)
        )

        opt_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        opt_frame.pack(fill="x", padx=5, pady=1)

        ctk.CTkLabel(opt_frame, text="Method:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.combo_method = ctk.CTkComboBox(
            opt_frame, values=VALID_METHODS, state="readonly", width=120
        )
        self.combo_method.set(self.config["method"])
        self.combo_method.grid(row=0, column=1, sticky="w", padx=(0, 5))
        _Tooltip(
            self.combo_method,
            "Segment: faster, ~1.5h, encodes each segment then joins.\nBatch: frame-exact, ~6-7h, uses select/aselect filter.",
        )

        ctk.CTkLabel(opt_frame, text="Encoder:").grid(row=1, column=0, sticky="w", padx=(0, 5))
        self.combo_encoder = ctk.CTkComboBox(
            opt_frame,
            values=VALID_ENCODERS,
            state="readonly",
            command=self._on_encoder_change,
            width=120,
        )
        self.combo_encoder.set(self.config["encoder"])
        self.combo_encoder.grid(row=1, column=1, sticky="w", padx=(0, 5))
        _Tooltip(
            self.combo_encoder,
            "h264_nvenc — NVIDIA GPU (GTX 1000+, RTX)\nh264_amf — AMD GPU (RX 400+, Ryzen APU)\nh264_mf — Windows Media Foundation (any GPU)\nlibx264 — CPU software encode (most compatible)",
        )

        self.btn_test_encoders = ctk.CTkButton(
            opt_frame, text="Test encoder", width=90, command=self._test_encoders
        )
        self.btn_test_encoders.grid(row=1, column=2, padx=(5, 0))

        self.lbl_encoder_desc = ctk.CTkLabel(opt_frame, text="", font=("", 10, "italic"))
        self.lbl_encoder_desc.grid(
            row=2, column=0, columnspan=4, sticky="w", padx=(0, 5), pady=(1, 0)
        )

        opt_frame.grid_columnconfigure(1, weight=0)
        self._on_encoder_change(self.config["encoder"])

        self.chk_force = ctk.CTkCheckBox(ctrl_frame, text="Force re-detect silence (ignore cache)")
        if self.config.get("force"):
            self.chk_force.select()
        self.chk_force.pack(anchor="w", padx=5, pady=(4, 1))

        self.chk_delete = ctk.CTkCheckBox(ctrl_frame, text="Delete downloaded source after success")
        if self.config.get("delete_after"):
            self.chk_delete.select()
        self.chk_delete.pack(anchor="w", padx=5, pady=(4, 1))

        self.chk_per_video_dir = ctk.CTkCheckBox(
            ctrl_frame,
            text="Create separate subdirectory for this video's project",
        )
        if self.config.get("per_video_dir"):
            self.chk_per_video_dir.select()
        self.chk_per_video_dir.pack(anchor="w", padx=5, pady=(4, 1))

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=4
        )

        # Action
        action_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        action_frame.pack(fill="x", padx=5, pady=(0, 6))

        # Action row: single-row pack in a left cluster.
        #   [Start] [Cancel] [Step / Complete ...]   — left-aligned, Step
        #   has a fixed max width (no fill/expand) so the text caps out
        #   instead of stretching across the whole row. Step sits
        #   immediately to the right of Cancel (4 px gap).
        left_cluster = ctk.CTkFrame(action_frame, fg_color="transparent")
        left_cluster.pack(side="left", fill="x", expand=True)

        self.btn_start = ctk.CTkButton(
            left_cluster,
            text="Start",
            command=self._start_pipeline,
            height=36,
            font=("", 13, "bold"),
            width=70,
        )
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_cancel = ctk.CTkButton(
            left_cluster,
            text="Cancel",
            command=self._cancel_pipeline,
            state="disabled",
            fg_color="#d32f2f",
            hover_color="#b71c1c",
            width=70,
        )
        self.btn_cancel.pack(side="left")

        # Step / Complete label, left-anchored, immediately after Cancel.
        # Fixed max width (no fill/expand) so the text caps out instead of
        # stretching across the whole row.
        self.lbl_status = ctk.CTkLabel(left_cluster, text="", anchor="w", width=400)
        self.lbl_status.pack(side="left", padx=(4, 0))

        self.bottom_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        self.bottom_frame.pack(fill="x", padx=5, pady=(0, 6))
        # 1:5 grid — progress bar at ~17% width (half of the previous 33%).
        # Row 0 holds the bar in col 0 and the live Elapsed/Remaining label
        # in col 1, left-anchored, immediately to the right of the bar.
        # Row 1 holds the Total wall-clock label, full width.
        self.bottom_frame.grid_columnconfigure(0, weight=1)
        self.bottom_frame.grid_columnconfigure(1, weight=5)
        self.progress = ctk.CTkProgressBar(
            self.bottom_frame,
            mode="determinate",
            height=10,
        )
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        # Live Elapsed/Remaining for the current phase. Same row as the
        # bar, left-anchored, immediately to the right of the bar.
        self.lbl_overall = ctk.CTkLabel(
            self.bottom_frame,
            text="",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        self.lbl_overall.grid(row=0, column=1, sticky="w", padx=(8, 0))
        # Total pipeline wall-clock, updated in real time. Row 1, full width.
        self.lbl_total = ctk.CTkLabel(
            self.bottom_frame,
            text="",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        self.lbl_total.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        # ── Right: Tabbed panel (Log + Waveform) ──
        right_frame = ctk.CTkFrame(self)
        right_frame.grid_rowconfigure(1, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid(row=0, column=2, sticky="nsew", padx=(3, 4), pady=4)

        self.right_tabs = ctk.CTkTabview(right_frame, height=100)
        self.right_tabs.grid(row=0, column=0, sticky="nsew", padx=3, pady=(3, 0))
        self.right_tabs.add("Log")
        self.right_tabs.add("Waveform")

        # Log tab — existing textbox.
        log_tab = self.right_tabs.tab("Log")
        log_tab.grid_rowconfigure(0, weight=1)
        log_tab.grid_columnconfigure(0, weight=1)
        self.txt_log = ctk.CTkTextbox(log_tab, wrap="word", state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # Waveform tab — image label, render button, status hint.
        wave_tab = self.right_tabs.tab("Waveform")
        wave_tab.grid_rowconfigure(1, weight=1)
        wave_tab.grid_columnconfigure(0, weight=1)
        self._build_waveform_tab(wave_tab)

    def _build_waveform_tab(self, parent):
        """Build the Waveform tab UI: a render button, status line, and
        an image label that displays the rendered waveform.

        The image is held in ``self._waveform_ctk_image`` (a CTkImage) so
        CustomTkinter keeps a strong reference (otherwise the image is
        garbage-collected and the label shows nothing).
        """
        # Row 0: button + status. Row 1: image area.
        controls = ctk.CTkFrame(parent, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        controls.grid_columnconfigure(1, weight=1)

        self.btn_render_wave = ctk.CTkButton(
            controls,
            text="Render preview",
            width=130,
            command=self._render_waveform_preview,
        )
        self.btn_render_wave.grid(row=0, column=0, padx=(0, 6))

        self.lbl_wave_status = ctk.CTkLabel(
            controls,
            text="No preview yet",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        self.lbl_wave_status.grid(row=0, column=1, sticky="ew")

        # Image area. Use a CTkLabel; swap the image after each render.
        self._waveform_ctk_image: ctk.CTkImage | None = None
        self.lbl_wave_image = ctk.CTkLabel(parent, text="", anchor="nw")
        self.lbl_wave_image.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))

        # Per-tab state.
        self._waveform_render_token = 0
        self._waveform_running = False

    def _add_slider(
        self,
        parent,
        label: str,
        key: str,
        min_v: float,
        max_v: float,
        current: float,
        tooltip: str = "",
    ):
        """Add a labelled slider row with editable value field and default button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(2, 0))

        lbl = ctk.CTkLabel(row, text=label, width=150, anchor="w")
        lbl.pack(side="left")
        if tooltip:
            _Tooltip(lbl, tooltip)

        slider = ctk.CTkSlider(
            row, from_=min_v, to=max_v, number_of_steps=round((max_v - min_v) * 10)
        )
        slider.set(current)
        slider.pack(side="left", fill="x", expand=True, padx=(0, 8))

        entry_val = ctk.CTkEntry(row, width=65, justify="right")
        entry_val.insert(0, f"{current:.1f}")
        entry_val.pack(side="right")

        btn_default = ctk.CTkButton(
            row,
            text="D",
            width=28,
            height=24,
            font=("", 10, "bold"),
            command=lambda k=key, d=CONFIG_DEFAULTS.get(key, current), sv=slider, ev=entry_val: (
                self._reset_default(d, sv, ev, k)
            ),
        )
        btn_default.pack(side="right", padx=(4, 0))

        slider._entry_val = entry_val
        setattr(self, f"_slider_{key}", slider)

        def on_change(v, k=key, ev=entry_val):
            ev.delete(0, "end")
            ev.insert(0, f"{float(v):.1f}")
            self.config[k] = round(float(v), 1)

        def on_entry_confirm(event=None, sv=slider, mn=min_v, mx=max_v, k=key):
            try:
                val = float(entry_val.get().replace(",", "."))
                val = max(mn, min(mx, val))
                val = round(val, 1)
                sv.set(val)
                self.config[k] = val
                entry_val.delete(0, "end")
                entry_val.insert(0, f"{val:.1f}")
            except ValueError:
                entry_val.delete(0, "end")
                entry_val.insert(0, f"{sv.get():.1f}")

        entry_val.bind("<Return>", on_entry_confirm)
        entry_val.bind("<FocusOut>", on_entry_confirm)
        slider.configure(command=on_change)

    def _reset_default(self, default: float, slider, entry, key: str):
        slider.set(default)
        entry.delete(0, "end")
        entry.insert(0, f"{default:.1f}")
        self.config[key] = default

    def _sync_slider_entries(self):
        for key in ("threshold", "min_silence", "margin"):
            slider = getattr(self, f"_slider_{key}", None)
            if slider and hasattr(slider, "_entry_val"):
                try:
                    val = float(slider._entry_val.get().replace(",", "."))
                    self.config[key] = round(val, 1)
                except ValueError:
                    pass

    # ── Dialogs & Events ─────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.ts"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.entry_input.delete(0, "end")
            self.entry_input.insert(0, path)
            self._update_file_info(Path(path))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self.entry_output.delete(0, "end")
            self.entry_output.insert(0, path)

    def _on_theme_change(self, choice):
        ctk.set_appearance_mode(choice)
        self.config["theme"] = choice

    ENCODER_DESCRIPTIONS: ClassVar[dict[str, str]] = {
        "h264_nvenc": "NVIDIA NVENC (GTX 1000+, RTX)",
        "h264_amf": "AMD AMF (RX 400+, Ryzen APU)",
        "h264_mf": "Media Foundation (any GPU, Windows only)",
        "libx264": "CPU software encode (most compatible)",
    }

    def _on_encoder_change(self, choice):
        self.config["encoder"] = choice
        desc = self.ENCODER_DESCRIPTIONS.get(choice, "")
        self.lbl_encoder_desc.configure(text=desc)

    def _test_encoders(self):
        enc = self.combo_encoder.get()
        if self._test_running:
            self._log("Test already running")
            return
        self._test_running = True
        self._log(f"Testing encoder: {enc} ...")
        self.btn_test_encoders.configure(state="disabled", text="Testing...")

        def _run():
            try:
                ok = check_encoder(enc)
                self._log(f"  {enc}: {'[OK]' if ok else 'NO'}")
            except FileNotFoundError:
                self._log(f"  {enc}: ffmpeg not found in PATH")
            except Exception as e:
                self._log(f"  {enc}: ERROR ({e})")
                logger.exception("Encoder test crashed")
            finally:
                self._test_running = False
                self.after(
                    0, lambda: self.btn_test_encoders.configure(state="normal", text="Test encoder")
                )

        threading.Thread(target=_run, daemon=True).start()

    def _update_file_info(self, path: Path):
        if not path.exists():
            return
        self.lbl_file.configure(text=f"File: {path.name}")
        size = path.stat().st_size
        self.lbl_size.configure(text=f"Size: {self._fmt_size(size)}")
        self.lbl_duration.configure(text="Duration: ...")

        def _get_dur():
            dur = get_video_duration(path)
            if dur:
                self.after(
                    0,
                    lambda d=dur: self.lbl_duration.configure(
                        text=f"Duration: {self._fmt_time(d)}"
                    ),
                )

        threading.Thread(target=_get_dur, daemon=True).start()

    # ── Pipeline ─────────────────────────────────────────────────

    def _start_pipeline(self):
        if self.running:
            return

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self._log("[ERROR] ffmpeg/ffprobe not found in PATH")
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg and ffprobe are required to process video.\n\n"
                "Install: winget install Gyan.FFmpeg\n"
                "Or run: setup.ps1 (Windows)",
            )
            return

        self._set_running(True)
        self._cancel_event.clear()
        self.progress.set(0)
        self.lbl_status.configure(text="Starting...")

        # Sync slider entries → config (in case FocusOut didn't fire)
        self._sync_slider_entries()

        # Read controls
        input_raw = self.entry_input.get().strip()
        output_dir = Path(self.entry_output.get().strip() or "./compressed_videos")
        output_dir = output_dir.resolve()
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        force = bool(self.chk_force.get())
        per_video_dir = bool(self.chk_per_video_dir.get())

        self._ui_update_output(output_dir)

        self._log(
            f"Starting pipeline: input={input_raw}, output_dir={output_dir}, "
            f"method={method}, encoder={encoder}, force={force}, "
            f"threshold={self.config['threshold']}, "
            f"min_silence={self.config['min_silence']}, "
            f"margin={self.config['margin']}, "
            f"delete_after={bool(self.chk_delete.get())}, "
            f"per_video_dir={per_video_dir}"
        )

        threading.Thread(
            target=self._pipeline_worker,
            args=(input_raw, output_dir, method, encoder, force, per_video_dir),
            daemon=True,
        ).start()

    def _cancel_pipeline(self):
        if self.running:
            self._cancel_event.set()
            self._log("Cancelling... (will stop after current step)")

    def _set_running(self, state: bool):
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
            self.after(0, lambda: self.lbl_overall.configure(text=""))
            self.after(0, lambda: self.lbl_total.configure(text=""))

    def _pipeline_worker(
        self,
        input_raw: str,
        output_dir: Path,
        method: str,
        encoder: str,
        force: bool,
        per_video_dir: bool = False,
    ):
        # Wall-clock anchor for the overall-time label.
        self._pipeline_start = time.monotonic()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Download / resolve path
            self._ui_progress(0.0)
            self._ui_status("Step 1/3: Downloading / resolving video...", force=True)
            self._log("Step 1/3: Downloading / resolving video...")
            self._ui_overall_elapsed_only()

            def dl_prog(frac: float, text: str):
                self._ui_progress(frac * 0.05)
                self._ui_status(f"Step 1/3: Downloading... {text}")
                self._ui_overall_elapsed_only()

            try:
                download_result = download(
                    input_raw,
                    output_dir,
                    cancel_callback=lambda: self._cancel_event.is_set(),
                    progress_callback=dl_prog,
                )
                video_path = download_result.path
            except DownloadCancelledError:
                self._log("Download cancelled")
                self._ui_status("Cancelled", force=True)
                return
            except DownloadError as e:
                self._log(f"[ERROR] Download failed: {e}")
                self._ui_status(f"Failed: {e}", force=True)
                return

            # Step 1.5: Apply per-video project directory (if enabled).
            # Downloaded source is moved; local file is untouched but the
            # project dir is still used for WAV / JSON / compressed / log.
            if per_video_dir:
                project_dir = ensure_project_dir(
                    output_dir,
                    video_path.stem,
                    per_video_dir,
                )
                if project_dir != output_dir:
                    if download_result.is_downloaded:
                        video_path = move_into_project(video_path, project_dir)
                        self._log(f"Moved source into project dir: {video_path}")
                    output_dir = project_dir
                    self._ui_update_output(output_dir)
                    self._log(f"Project directory: {output_dir}")

            # Always track the final output dir in recent projects, so
            # users who toggle per_video_dir off still see their work
            # surfaces in the panel.
            self._add_to_recent_projects(output_dir)

            self._download_path = video_path if download_result.is_downloaded else None
            self._ui_update_file_info(video_path)
            src_size_bytes = video_path.stat().st_size
            file_size_mb = src_size_bytes // 1024 // 1024
            # Probe source duration synchronously so the final summary
            # has both size and duration. ffprobe on a local file is fast
            # (< 100ms typically); if it fails we show '?' in the summary.
            src_duration = get_video_duration(video_path)
            if download_result.is_downloaded:
                self._log(f"Downloaded: {input_raw} -> {video_path}")
            else:
                self._log(f"Download skipped (file already on disk): {video_path}")
            self._log(f"Size: {self._fmt_size(src_size_bytes)}")

            if method == "batch" and file_size_mb > 4096:
                self._log(
                    f"[WARN] File is {file_size_mb} MB — batch mode may use a lot of RAM. "
                    f"If it crashes, re-run with method=segment."
                )

            if self._cancel_event.is_set():
                self._ui_status("Cancelled", force=True)
                return

            # Step 2: Silence detection
            self._ui_progress(0.05)
            self._ui_status("Step 2/3: Detecting silence...", force=True)
            self._log(
                f"Step 2/3: Detecting silence "
                f"(threshold={self.config['threshold']}dB, "
                f"min_silence={self.config['min_silence']}s, "
                f"margin={self.config['margin']}s)..."
            )

            config = {
                "threshold": self.config["threshold"],
                "min_silence": self.config["min_silence"],
                "margin": self.config["margin"],
            }

            cache = None if force else load_silence_cache(video_path, output_dir, config)
            if cache is not None:
                silence_segments = cache
            else:
                silence_start = time.monotonic()

                def silence_prog(f: float):
                    elapsed = time.monotonic() - silence_start
                    remaining = elapsed / f - elapsed if f > 0.01 else 0
                    self._ui_progress(0.05 + f * 0.35)
                    self._ui_status(
                        f"Step 2/3: Silence... {f * 100:.0f}% "
                        f"({self._fmt_time(elapsed)}/{self._fmt_time(remaining)})"
                    )
                    self._ui_overall(elapsed, remaining, more_phases=True)

                silence_segments = detect_silence(
                    video_path,
                    **config,
                    output_dir=output_dir,
                    progress_callback=silence_prog,
                    cancel_callback=lambda: self._cancel_event.is_set(),
                )
                save_silence_cache(video_path, silence_segments, output_dir, config)
                self._log(f"Detected {len(silence_segments)} silence segments")

            if self._cancel_event.is_set():
                self._ui_status("Cancelled", force=True)
                return

            # Update info
            keep = generate_keep_segments(video_path, silence_segments)
            keep_dur = sum(e - s for s, e in keep)
            self._ui_info(
                f"Silence: {len(silence_segments)} segments\nKeep: {len(keep)} segments ({self._fmt_time(keep_dur)})"
            )

            # Step 3: Cut & concat
            self._ui_progress(0.4)
            self._ui_status("Step 3/3: Cutting and concatenating...", force=True)
            self._log(f"Step 3/3: Cutting & concatenating (method={method}, encoder={encoder})...")

            output_path = output_dir / f"{video_path.stem}_compressed.mp4"
            self._output_path = output_path

            self.after(0, lambda: self.lbl_encoder.configure(text=f"Encoder: {encoder}"))

            cut_start = time.monotonic()

            def concat_prog(f: float):
                elapsed = time.monotonic() - cut_start
                remaining = elapsed / f - elapsed if f > 0.01 else 0
                self._ui_progress(0.4 + f * 0.6)
                self._ui_status(
                    f"Step 3/3: Cutting... {f * 100:.0f}% "
                    f"({self._fmt_time(elapsed)}/{self._fmt_time(remaining)})"
                )
                # Phase 3 is the last one — no "more phases" after it.
                self._ui_overall(elapsed, remaining, more_phases=False)

            cut_and_concat(
                video_path,
                silence_segments,
                output_path,
                progress_callback=concat_prog,
                method=method,
                encoder=encoder,
                cancel_callback=lambda: self._cancel_event.is_set(),
            )

            self._output_path = None
            self._ui_progress(1.0)

            # ── Build the final summary ────────────────────────────
            # Pure function — builds the status line, log block,
            # popup body, and overall label from the four metrics.
            dst_size_bytes = output_path.stat().st_size
            total_elapsed = time.monotonic() - self._pipeline_start
            summary = _build_completion_summary(
                src_size_bytes=src_size_bytes,
                src_duration=src_duration,
                dst_size_bytes=dst_size_bytes,
                dst_duration=keep_dur,
                pipeline_seconds=total_elapsed,
                output_path=str(output_path),
            )

            # Status line — one-line headline so the user sees the result
            # without opening the popup. Format: "Complete! (23m 5s)" —
            # headline + total wall-clock in parentheses. Size and duration
            # go in the popup and the log block.
            self._ui_status(summary["status"], force=True)

            # Log — multi-line, delimited by '=' so the user can grep
            # for the end of a run.
            for line in summary["log_lines"]:
                self._log(line)

            # Clear the Elapsed/Remaining line — its job is done once
            # the status line carries the final pipeline time.
            self.after(0, lambda: self.lbl_overall.configure(text=""))
            # Freeze the Total wall-clock label at its final value.
            self._ui_total(total_elapsed)

            # Delete downloaded source if requested
            if bool(self.chk_delete.get()) and self._download_path is not None:
                try:
                    self._download_path.unlink()
                    self._log(f"Deleted source: {self._download_path}")
                except OSError as e:
                    self._log(f"[WARN] Could not delete source: {e}")
            self._download_path = None

            # Show completion popup
            self.after(0, lambda: messagebox.showinfo("Complete", summary["popup"]))

        except (CancelledError, SilenceCancelledError):
            self._log("Pipeline cancelled")
            self._ui_status("Cancelled", force=True)
        except SilenceDetectionError as e:
            self._log(f"[ERROR] Silence detection failed: {e}")
            self._ui_status(f"Failed: {e}", force=True)
        except ConcatError as e:
            self._log(f"[ERROR] {e}")
            self._ui_status(f"Failed: {e}", force=True)
        except Exception as e:
            self._log(f"[ERROR] Unexpected: {e}")
            logger.exception("Pipeline error")
            self._ui_status(f"Error: {e}", force=True)
        finally:
            self._output_path = None
            self._download_path = None
            self.after(0, lambda: self._set_running(False))

    # ── Recent Projects ───────────────────────────────────────────

    def _render_recent_projects(self):
        """Rebuild the Recent Projects sub-section from self.config.

        Prunes entries whose directory no longer exists. Rows have a label
        (project name + tooltip with full path) and a trash button that
        asks for confirmation before deleting the whole subdirectory.
        """
        for child in self.recent_frame.winfo_children():
            child.destroy()
        pruned = prune_recent_projects(self.config.get("recent_projects", []))
        if pruned != self.config.get("recent_projects", []):
            self.config["recent_projects"] = pruned
        if not pruned:
            ctk.CTkLabel(
                self.recent_frame,
                text="(no recent projects)",
                text_color=("gray50", "gray60"),
                anchor="w",
            ).pack(fill="x", padx=5, pady=2)
            return
        for path_str in pruned:
            row = ctk.CTkFrame(self.recent_frame, fg_color="transparent")
            row.pack(fill="x", padx=2, pady=1)
            display = Path(path_str).name or path_str
            lbl = ctk.CTkLabel(
                row,
                text=_truncate(display, _RECENT_NAME_MAX),
                anchor="w",
                cursor="hand2",
            )
            lbl.pack(side="left", fill="x", expand=True, padx=(3, 2))
            lbl.bind(
                "<Button-1>",
                lambda e, p=path_str: self._open_in_explorer(p),
            )
            _Tooltip(lbl, path_str)
            del_btn = ctk.CTkButton(
                row,
                text="X",
                width=22,
                height=22,
                fg_color=("gray70", "gray30"),
                hover_color=("#c0392b", "#922B21"),
                text_color=("gray10", "gray90"),
                command=lambda p=path_str: self._delete_recent_project(p),
            )
            del_btn.pack(side="right", padx=(0, 3))

    def _add_to_recent_projects(self, project_path):
        """Add or move ``project_path`` to the top of the recent list (max 5).

        Also persists to settings.json eagerly so a GUI crash/kill does not
        lose the list. (Final save still happens in _on_close, but we
        want every pipeline run's project to survive a restart.)
        """
        if not project_path:
            return
        path_str = str(project_path)
        self.config["recent_projects"] = add_recent_project(
            self.config.get("recent_projects", []),
            path_str,
        )
        self._render_recent_projects()
        try:
            self._save_settings()
        except Exception as e:
            logger.warning("Failed to save settings after adding recent project: %s", e)

    def _delete_recent_project(self, path_str: str):
        """Confirm with the user, then recursively delete the project dir."""
        if self.running:
            self._log("Cannot delete a project while pipeline is running")
            return
        path = Path(path_str)
        if not path.is_dir():
            self._log(f"Project no longer exists, dropping from list: {path_str}")
            self.config["recent_projects"] = [
                p for p in self.config.get("recent_projects", []) if p != path_str
            ]
            self._render_recent_projects()
            return
        size_mb = self._dir_size_mb(path)
        msg = (
            f"Delete project '{path.name}' and ALL its contents?\n\n"
            f"Location: {path}\n"
            f"Approx size: {size_mb:.1f} MB\n\n"
            f"This will permanently remove the source video (if downloaded), "
            f"the compressed output, the audio cache, the silence cache, "
            f"and the log file.\n\n"
            f"This cannot be undone."
        )
        ok = messagebox.askyesno("Delete project?", msg, icon="warning")
        if not ok:
            return
        try:
            shutil.rmtree(path)
            self._log(f"Deleted project: {path}")
        except OSError as e:
            self._log(f"[ERROR] Failed to delete {path}: {e}")
            messagebox.showerror("Delete failed", f"Could not delete {path}:\n{e}")
            return
        self.config["recent_projects"] = [
            p for p in self.config.get("recent_projects", []) if p != path_str
        ]
        self._render_recent_projects()

    def _dir_size_mb(self, path: Path) -> float:
        """Approximate directory size in MB. Fast — sums stat().st_size."""
        total = 0
        try:
            for p in path.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total / 1024 / 1024

    def _open_in_explorer(self, path_str: str):
        """Open the project directory in the platform's file manager."""
        path = Path(path_str)
        if not path.is_dir():
            messagebox.showwarning(
                "Folder not found",
                f"Directory no longer exists:\n{path_str}",
            )
            self.config["recent_projects"] = [
                p for p in self.config.get("recent_projects", []) if p != path_str
            ]
            self._render_recent_projects()
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as e:
            self._log(f"[ERROR] Could not open {path}: {e}")

    # ── UI Helpers ───────────────────────────────────────────────

    def _ui_progress(self, value: float):
        self.after(0, lambda: self.progress.set(max(0.0, min(1.0, value))))

    def _ui_overall(self, phase_elapsed: float, phase_remaining: float, more_phases: bool):
        """Update the live Elapsed/Remaining line in bottom_frame (the
        label sitting immediately to the right of the progress bar) and
        the Total wall-clock label below the bar.

        'phase_remaining' is the ETA for the CURRENT phase; if more
        phases follow, we append ' + ?' to make clear that the other
        phases' durations are unknown. During phase 1 (no progress
        callback) the label is updated with just elapsed (remaining='?').
        """
        if self._pipeline_start is None:
            return
        total_elapsed = time.monotonic() - self._pipeline_start
        if phase_remaining <= 0:
            tail = "?" if more_phases else "—"
        else:
            tail = (
                f"~{self._fmt_time(phase_remaining)} + ?"
                if more_phases
                else f"~{self._fmt_time(phase_remaining)}"
            )
        text = f"Elapsed: {self._fmt_time(total_elapsed)} | Remaining: {tail}"
        self.after(0, lambda: self.lbl_overall.configure(text=text))
        self._ui_total(total_elapsed)

    def _ui_overall_elapsed_only(self):
        """Update the overall label with only elapsed time (no remaining
        estimate available — used during phase 1 download when progress
        is indeterminate)."""
        if self._pipeline_start is None:
            return
        total_elapsed = time.monotonic() - self._pipeline_start
        self.after(
            0,
            lambda: self.lbl_overall.configure(
                text=f"Elapsed: {self._fmt_time(total_elapsed)} | Remaining: ?"
            ),
        )
        self._ui_total(total_elapsed)

    def _ui_total(self, total_elapsed: float):
        """Update the Total wall-clock label below the progress bar."""
        self.after(
            0,
            lambda: self.lbl_total.configure(text=Stream2VideoGUI._fmt_total_label(total_elapsed)),
        )

    @staticmethod
    def _fmt_total_label(total_elapsed: float) -> str:
        """Format the Total label — 'Total: X' where X is the wall-clock
        pipeline duration. Pure helper, easy to unit-test."""
        return f"Total: {Stream2VideoGUI._fmt_time(total_elapsed)}"

    _STATUS_MAX = 50

    def _ui_status(self, text: str, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_status_update < 0.5:
            return
        self._last_status_update = now
        if len(text) > self._STATUS_MAX:
            text = text[: self._STATUS_MAX - 1] + "…"
        self.after(0, lambda: self.lbl_status.configure(text=text))

    def _ui_info(self, text: str):
        self.after(0, lambda t=text: self.lbl_silence.configure(text=t))

    def _ui_update_file_info(self, path: Path):
        self.after(0, lambda: self._update_file_info(path))

    def _ui_update_output(self, path: Path):
        self.after(0, lambda p=path: self.lbl_output.configure(text=f"Output: {p}"))

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _poll_log_queue(self):
        """Periodically drain the log queue into the textbox."""
        try:
            while True:
                try:
                    msg = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except Exception as e:
            logger.exception("Log queue poller crashed: %s", e)
        self.after(100, self._poll_log_queue)

    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("stream2video").addHandler(handler)

    # ── Waveform tab ────────────────────────────────────────────

    def _render_waveform_preview(self):
        """Extract audio (if needed), run silence detection with the
        current slider values, and render the waveform with overlay.

        Runs on a background thread so the GUI stays responsive during
        the (potentially long) first extract. Re-runs are debounced by
        ``_waveform_render_token`` — if the user clicks "Render" again
        before the previous run finishes, the older one is invalidated.
        """
        if self._waveform_running:
            self._log("Waveform render already running")
            return
        if self.running:
            self._log("Cannot preview waveform while pipeline is running")
            return

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

        # Output dir: where the cached WAV will live. Same resolution as
        # the pipeline (resolve + per-video dir if enabled).
        out_raw = self.entry_output.get().strip() or "./compressed_videos"
        out_dir = Path(out_raw).expanduser().resolve()
        if bool(self.chk_per_video_dir.get()):
            out_dir = out_dir / in_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)

        wav_path = out_dir / f"{in_path.stem}_audio.wav"
        token = self._waveform_render_token + 1
        self._waveform_render_token = token
        self._waveform_running = True
        self.btn_render_wave.configure(state="disabled", text="Rendering...")
        self.lbl_wave_status.configure(text="Preparing audio...")
        self._log("Waveform preview: preparing audio...")

        def _run():
            try:
                # Phase 1: ensure WAV exists.
                if not wav_path.is_file() or wav_path.stat().st_mtime < in_path.stat().st_mtime:
                    self.after(
                        0, lambda: self.lbl_wave_status.configure(text="Extracting audio...")
                    )
                    self._log(f"  Extracting {in_path.name} -> {wav_path.name}...")
                    from stream2video.silence import _extract_audio_wav

                    _extract_audio_wav(in_path, wav_path)
                if token != self._waveform_render_token:
                    return

                # Phase 2: silence detection.
                self.after(0, lambda: self.lbl_wave_status.configure(text="Detecting silence..."))
                self._log(
                    f"  Detecting silence (threshold={config['threshold']}dB, "
                    f"min_silence={config['min_silence']}s, margin={config['margin']}s)..."
                )
                from stream2video.silence import detect_silence, save_silence_cache

                segments = detect_silence(
                    wav_path,
                    threshold=config["threshold"],
                    min_silence=config["min_silence"],
                    margin=config["margin"],
                    progress_callback=lambda f: self.after(
                        0,
                        lambda: self.lbl_wave_status.configure(
                            text=f"Detecting silence... {int(f * 100)}%"
                        ),
                    ),
                )
                # Save to cache so a subsequent real pipeline run with
                # these same params is instant.
                save_silence_cache(wav_path, segments, out_dir, config)
                if token != self._waveform_render_token:
                    return

                # Phase 3: read peaks + render.
                self.after(0, lambda: self.lbl_wave_status.configure(text="Rendering..."))
                duration = wav_path.stat().st_size / (16000 * 2)  # 16kHz mono s16le
                peaks = read_waveform_peaks(wav_path, target_buckets=800)
                img = render_waveform_image(
                    peaks,
                    width=800,
                    height=200,
                    total_duration=duration,
                    silence_segments=segments,
                    title=f"{in_path.name}  •  {len(segments)} silences",
                )
                if token != self._waveform_render_token:
                    return

                self.after(0, lambda: self._apply_waveform_image(img, duration, len(segments)))
                self._log(
                    f"  Waveform ready: {len(segments)} silence segments, "
                    f"{Stream2VideoGUI._fmt_time(duration)} duration"
                )
            except Exception as e:
                logger.exception("Waveform render failed")
                self.after(0, lambda err=e: self.lbl_wave_status.configure(text=f"Error: {err}"))
                self._log(f"[ERROR] Waveform render failed: {e}")
            finally:
                self._waveform_running = False
                self.after(
                    0,
                    lambda: self.btn_render_wave.configure(state="normal", text="Render preview"),
                )

        threading.Thread(target=_run, daemon=True).start()

    def _apply_waveform_image(self, img: Image.Image, duration: float, n_segments: int):
        """Swap the displayed image and update the status label.

        Keeps a strong reference on ``self._waveform_ctk_image`` —
        CustomTkinter's ``CTkLabel.configure(image=...)`` only borrows
        the image, so without the ref Python GC's the underlying Pillow
        object and the label goes blank.
        """
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self._waveform_ctk_image = ctk_img
        self.lbl_wave_image.configure(image=ctk_img, text="")
        self.lbl_wave_status.configure(
            text=f"{n_segments} silences • {Stream2VideoGUI._fmt_clock_time(duration)}"
        )

    # ── Utilities ────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(bytez: int) -> str:
        size = float(bytez)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @staticmethod
    def _fmt_time(secs: float) -> str:
        total = int(secs)
        d, r = divmod(total, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        if d:
            return f"{d}d {h}h {m}m {s}s"
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    @staticmethod
    def _fmt_clock_time(secs: float | None) -> str:
        """Format a duration as HH:MM:SS (or D:HH:MM:SS if >= 24h),
        zero-padded. Used in the final summary so '06:04:12 -> 00:34:11'
        is scannable. Returns '?' for None (e.g. ffprobe failed)."""
        if secs is None or secs < 0:
            return "?"
        total = int(secs)
        d, r = divmod(total, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        if d:
            return f"{d}:{h:02d}:{m:02d}:{s:02d}"
        return f"{h:02d}:{m:02d}:{s:02d}"

    # ── Settings Persistence ─────────────────────────────────────

    def _settings_path(self) -> Path:
        portable = Path(__file__).parent.parent / "_portable"
        if portable.exists():
            return portable / "settings.json"
        return Path(__file__).parent.parent / "gui_settings.json"

    def _save_settings(self):
        self.config["input_path"] = self.entry_input.get().strip()
        self.config["output_dir"] = self.entry_output.get().strip()
        self.config["method"] = self.combo_method.get()
        self.config["encoder"] = self.combo_encoder.get()
        self.config["force"] = bool(self.chk_force.get())
        self.config["delete_after"] = bool(self.chk_delete.get())
        self.config["per_video_dir"] = bool(self.chk_per_video_dir.get())
        self.config["theme"] = self.combo_theme.get()
        self.config["window_geometry"] = self.geometry()
        try:
            with open(self._settings_path(), "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            self._log(f"[WARN] Could not save settings: {e}")

    def _load_settings(self):
        sp = self._settings_path()
        if not sp.exists():
            return
        try:
            with open(sp, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load settings: %s", e)
            return
        if not isinstance(loaded, dict):
            logger.warning("Settings file is not a JSON object; ignoring")
            return
        for key, value in loaded.items():
            coerced = coerce_typed_value(key, value)
            if coerced is not None:
                self.config[key] = coerced
            else:
                logger.debug("Dropping settings[%r] with wrong type: %r", key, value)

    def _restore_defaults(self):
        self.config = effective_defaults()
        ctk.set_appearance_mode(self.config["theme"])
        self.combo_theme.set(self.config["theme"])
        self.entry_input.delete(0, "end")
        self.entry_output.delete(0, "end")

        self.combo_method.set(self.config["method"])
        self.combo_encoder.set(self.config["encoder"])
        self._on_encoder_change(self.config["encoder"])
        self._set_checkbox(self.chk_force, self.config["force"])
        self._set_checkbox(self.chk_delete, self.config["delete_after"])
        self._set_checkbox(self.chk_per_video_dir, self.config["per_video_dir"])
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{min(1080, sw - 40)}x{min(680, sh - 60)}")
        for key in ("threshold", "min_silence", "margin"):
            slider = getattr(self, f"_slider_{key}", None)
            if slider:
                val = self.config[key]
                slider.set(val)
                ev = getattr(slider, "_entry_val", None)
                if ev:
                    ev.delete(0, "end")
                    ev.insert(0, f"{val:.1f}")
        self.lbl_output.configure(text="Output: —")
        self.lbl_file.configure(text="File: —")
        self.lbl_size.configure(text="Size: —")
        self.lbl_duration.configure(text="Duration: —")
        self.lbl_silence.configure(text="Silence: —")
        self.lbl_encoder.configure(text="Encoder: —")
        self._save_settings()
        self._log("Settings restored to defaults")

    @staticmethod
    def _set_checkbox(checkbox, value: bool) -> None:
        """Set a CTkCheckBox to True/False (deselect/select) — same widget,
        different state. CTk's select/deselect don't accept a value, so
        this is a tiny helper to keep callers readable."""
        if value:
            checkbox.select()
        else:
            checkbox.deselect()

    def _save_user_defaults(self):
        """Snapshot the current tunable GUI values to user_defaults.json.
        This is the per-user "factory defaults" file — it overrides
        CONFIG_DEFAULTS on next startup. Per-session state (output_dir,
        recent_projects, input_path) is intentionally NOT saved here."""
        try:
            self._sync_slider_entries()
        except Exception:
            pass
        snapshot = {
            "threshold": float(self.config["threshold"]),
            "min_silence": float(self.config["min_silence"]),
            "margin": float(self.config["margin"]),
            "method": self.combo_method.get(),
            "encoder": self.combo_encoder.get(),
            "force": bool(self.chk_force.get()),
            "delete_after": bool(self.chk_delete.get()),
            "per_video_dir": bool(self.chk_per_video_dir.get()),
            "theme": self.combo_theme.get(),
        }
        try:
            save_user_defaults(snapshot)
        except Exception as e:
            self._log(f"[WARN] Could not save user defaults: {e}")
            return
        self._log(f"Saved current settings as user defaults ({user_defaults_path()})")

    def _copy_cli_command(self):
        self._sync_slider_entries()
        inp = self.entry_input.get().strip()
        out_raw = self.entry_output.get().strip() or "./compressed_videos"
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        force = bool(self.chk_force.get())
        delete_after = bool(self.chk_delete.get())

        out_path = Path(out_raw).expanduser()
        config_path = None
        try:
            out_path.mkdir(parents=True, exist_ok=True)
            config_path = (out_path / "stream2video_cli_config.yaml").resolve()
            config_yaml = (
                f"threshold: {self.config['threshold']}\n"
                f"min_silence: {self.config['min_silence']}\n"
                f"margin: {self.config['margin']}\n"
            )
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(config_yaml)
        except Exception as e:
            self._log(f"[WARN] Could not write CLI config: {e}")
            config_path = None

        parts = ["stream2video"]
        if inp:
            parts.append(shlex.quote(inp))
        parts.extend(["-o", shlex.quote(str(out_path))])
        if config_path is not None:
            parts.extend(["-c", shlex.quote(str(config_path))])
        parts.extend(["--method", method, "--encoder", encoder])
        if force:
            parts.append("-f")
        if delete_after:
            parts.append("--delete-after")
        cmd = " ".join(parts)
        self.clipboard_clear()
        self.clipboard_append(cmd)
        if config_path is not None:
            self._log(f"CLI command copied. Config written to: {config_path}")
        else:
            self._log(f"CLI command copied (config NOT written — see warning): {cmd}")
        self._log(f"  {cmd}")

    def _on_close(self):
        if self.running:
            answer = messagebox.askyesno(
                "Quit?",
                "Pipeline is running. Stop and quit?",
            )
            if not answer:
                return
        self._cancel_event.set()
        proc = get_active_process()
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        # Clean up incomplete output file
        if self._output_path is not None and self._output_path.exists():
            try:
                self._output_path.unlink()
                logger.info(f"Cleaned up incomplete output: {self._output_path}")
            except OSError:
                pass
        try:
            self._save_settings()
        except Exception as e:
            logger.warning("Failed to save settings on close: %s", e)
        self.destroy()


def main():
    app = Stream2VideoGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
