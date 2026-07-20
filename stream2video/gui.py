"""stream2video GUI — cross-platform desktop application."""

import logging
import math
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tkinter import Event, StringVar, TclError, filedialog, messagebox
from typing import Any, ClassVar

import customtkinter as ctk

from stream2video.concat import (
    check_encoder,
)
from stream2video.config import (
    CONFIG_DEFAULTS,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_QUALITIES,
    VALID_THEMES,
    effective_defaults,
    save_user_defaults,
    settings_path,
    user_defaults_path,
)
from stream2video.download import (
    DownloadProgress,
)
from stream2video.formatters import (
    fmt_clock_time,
    fmt_size,
    fmt_speed,
    fmt_time,
    fmt_total_label,
    fmt_zoom_text,
)
from stream2video.gui_helpers import (
    STATUS_MAX,
    build_cli_command,
    build_eta_tail,
    build_overall_line,
    should_update_status,
    truncate_status,
)
from stream2video.gui_log_handler import QueueHandler
from stream2video.gui_platform import (
    dir_size_mb,
    fit_to_screen,
    is_previewable_input,
    open_in_file_manager,
)
from stream2video.gui_settings import load_settings as _load_settings_from_disk
from stream2video.gui_settings import save_settings as _save_settings_to_disk
from stream2video.gui_widgets import Tooltip as _Tooltip
from stream2video.paths import (
    RECENT_NAME_MAX,
    add_recent_project,
    prune_recent_projects,
    truncate_recent_name,
)
from stream2video.silence import (
    SilenceDetectionError,
    SilenceSegment,
    apply_margin,
    detect_silence_stream,
    load_silence_cache,
)
from stream2video.utils import cancel_process, get_active_process, get_video_duration
from stream2video.waveform import (
    DB_AXIS_WIDTH,
    read_peaks_from_stream,
    render_waveform_image,
    slice_peaks_by_time,
)

logger = logging.getLogger("stream2video.gui")


# ``_Tooltip`` and ``QueueHandler`` were extracted to ``gui_widgets.py``
# and ``gui_log_handler.py`` respectively as part of the Этап 10
# incremental refactor — they're imported above (Tooltip as _Tooltip
# for back-compat). The class bodies that used to live here are gone;
# see those modules for the implementations.
#
# ``_build_completion_summary`` was extracted to ``gui_helpers.py`` as
# ``build_completion_summary`` (back-compat alias imported above).
# Pure function — no Tk — so it can be unit-tested without
# instantiating the GUI.


class Stream2VideoGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("stream2video")

        self.running = False
        self._cancel_event = threading.Event()
        self._test_running = False
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
        # In-memory live silence segments, keyed by source video path.
        # Updated by the pipeline worker's on_segment callback as new
        # segments are detected; read by the waveform popup's poller for
        # near-real-time overlays. Cleared on GUI exit (process end).
        # Lock protects concurrent updates from the pipeline's stderr
        # drain thread and reads from the popup's poller thread.
        self._live_segments: dict[Path, list[SilenceSegment]] = {}
        self._live_segments_lock = threading.Lock()
        # Waveform view state — populated by the renderer when peaks
        # arrive, modified by the zoom/pan controls. Cleared when the
        # popup closes (see `_on_waveform_close`).
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
        win_w, win_h = self._fit_to_screen(sw, sh)
        self.minsize(
            max(1, min(1000, sw - 40)),
            max(1, min(620, sh - 60)),
        )

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

    def _wave_window_alive(self) -> bool:
        """True if the waveform popup exists and is still on-screen.

        Centralised so callers don't repeat ``self._wave_window is None or
        not self._wave_window.winfo_exists()`` — and so a programmatic
        caller (test / REPL) that invokes a waveform method before
        ``__init__`` has set ``self._wave_window`` gets a clean False
        rather than ``AttributeError``.
        """
        win = getattr(self, "_wave_window", None)
        return win is not None and win.winfo_exists()

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
        self.input_var = StringVar()
        self.entry_input = ctk.CTkEntry(
            row,
            textvariable=self.input_var,
            placeholder_text="video.mp4 or https://...",
        )
        self.entry_input.pack(side="left", fill="x", expand=True)
        if self.config.get("input_path"):
            self.input_var.set(self.config["input_path"])
        # Refresh the Waveform button whenever the input path changes
        # (typing, paste, Browse, programmatic). Only enabled when the
        # field points at a viewable local file.
        self.input_var.trace_add("write", lambda *_: self._update_waveform_button_state())
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

        # Video quality preset — bitrate (HW encoders) / CRF (libx264).
        ctk.CTkLabel(opt_frame, text="Video quality:").grid(
            row=3, column=0, sticky="w", padx=(0, 5)
        )
        self.combo_video_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_QUALITIES, state="readonly", width=120
        )
        self.combo_video_quality.set(self.config["video_quality"])
        self.combo_video_quality.grid(row=3, column=1, sticky="w", padx=(0, 5))
        _Tooltip(
            self.combo_video_quality,
            "Preset for the encode step.\nhigh — 10000k / CRF 18 (large files, best quality)\nmedium — 7000k / CRF 23 (default)\nlow — 3500k / CRF 28 (small files)",
        )

        # Audio quality preset — bitrate of the AAC encode. Kept separate
        # from video_quality so a 192k/256k source is not silently downgraded
        # to 128k (P0.3 in the fix plan).
        ctk.CTkLabel(opt_frame, text="Audio quality:").grid(
            row=4, column=0, sticky="w", padx=(0, 5)
        )
        self.combo_audio_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_QUALITIES, state="readonly", width=120
        )
        self.combo_audio_quality.set(self.config.get("audio_quality", "medium"))
        self.combo_audio_quality.grid(row=4, column=1, sticky="w", padx=(0, 5))
        _Tooltip(
            self.combo_audio_quality,
            "Bitrate for the AAC audio encode.\nhigh — 256k (best quality)\nmedium — 192k (default)\nlow — 128k (smaller files)",
        )

        # Download quality preset — Twitch/YouTube resolution cap. Ignored
        # for local files (the source file is used as-is).
        ctk.CTkLabel(opt_frame, text="Download quality:").grid(
            row=5, column=0, sticky="w", padx=(0, 5)
        )
        self.combo_download_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_DOWNLOAD_QUALITIES, state="readonly", width=120
        )
        self.combo_download_quality.set(self.config["download_quality"])
        self.combo_download_quality.grid(row=5, column=1, sticky="w", padx=(0, 5))
        _Tooltip(
            self.combo_download_quality,
            "Max resolution to download from Twitch/YouTube.\nbest — highest available (default)\n1080p / 720p / 480p / 360p — cap height\nIgnored for local files.",
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

        # ── Right: Log panel + Waveform button ──
        # The waveform preview is opened in its own Toplevel window when
        # the user clicks "Waveform" (see ``_open_waveform_window``). The
        # popup auto-renders the preview on open; no render button.
        right_frame = ctk.CTkFrame(self)
        right_frame.grid_rowconfigure(2, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid(row=0, column=2, sticky="nsew", padx=(3, 4), pady=4)

        header = ctk.CTkFrame(right_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=4, pady=(3, 0))
        header.grid_columnconfigure(0, weight=1)
        log_header = ctk.CTkLabel(
            header, text="Log", anchor="w", font=ctk.CTkFont(size=12, weight="bold")
        )
        log_header.grid(row=0, column=0, sticky="w")
        self.btn_waveform = ctk.CTkButton(
            header,
            text="Waveform",
            width=130,
            command=self._open_waveform_window,
        )
        self.btn_waveform.grid(row=0, column=1, padx=(6, 0))
        # Sync the disabled state with whatever's in the input field
        # (which may have been pre-filled from saved config).
        self._update_waveform_button_state()

        self.txt_log = ctk.CTkTextbox(right_frame, wrap="word", state="disabled")
        self.txt_log.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)

    def _open_waveform_window(self):
        """Open the waveform preview in a Toplevel window; auto-renders on open.

        Layout (top to bottom):
          0: Status line (silence count, duration, current view)
          1: Zoom/pan controls (zoom buttons -/1x/+, pan buttons </>, position slider)
          2: Image area (weight=1, expands to fill remaining space)

        If the popup already exists, focus it instead of creating a new one.
        On close (X), refs are nulled so a re-open re-creates the widgets.
        The render runs automatically on open — no manual render button.
        """
        # Defensive guard: the Waveform button is normally disabled when no
        # viewable file is selected, but log if a click somehow gets through
        # (e.g. via keyboard accelerator / programmatic invoke).
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
        # Re-render the waveform when the popup is resized so the image
        # always fills the available area. Bound on the toplevel (not
        # the image label) so we get the window's actual size; the
        # handler debounces via ``after_idle``.
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
        win.grid_rowconfigure(2, weight=1)
        win.grid_columnconfigure(0, weight=1)
        self.lbl_wave_image = ctk.CTkLabel(win, text="", anchor="nw")
        self.lbl_wave_image.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 8))
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

    def _on_waveform_close(self):
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

    def _on_waveform_window_configure(self, event):
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
        """Image size to use for the next render, derived from the
        current popup window size.

        The popup is laid out in three rows: status (~30 px), zoom/pan
        controls (~30 px), and the image row (expanding). A small
        vertical reservation is taken off the window height to leave
        room for the two fixed rows plus the image row's own pady;
        horizontal padx is subtracted from the width. A 800x200
        fallback is used when the window has not been laid out yet
        (e.g., first render immediately after the popup is created).
        """
        fallback = (800, 200)
        if not self._wave_window_alive():
            return fallback
        # _wave_window_alive() above guarantees self._wave_window is set,
        # but mypy can't follow the helper's return type — narrow locally
        # so the .winfo_width/height() below don't union-attr.
        assert self._wave_window is not None
        win_w = self._wave_window.winfo_width()
        win_h = self._wave_window.winfo_height()
        if win_w < 100 or win_h < 100:
            return fallback
        # Status row + controls row + inter-row spacing + image pady
        # (2 top + 8 bottom) ≈ 80 px; padx is 8 on each side.
        reserved_h = 80
        reserved_w = 16
        w = max(200, win_w - reserved_w)
        h = max(80, win_h - reserved_h)
        return (w, h)

    def _schedule_waveform_threshold_re_render(self) -> None:
        """Schedule a debounced re-render of the waveform popup so the
        threshold line tracks the slider's current value.

        CTkSlider's ``command`` fires on every step of a drag, and
        PIL rendering at ~60 Hz is wasteful — and worse, it can fall
        behind the cursor and leave the previous threshold value
        visible until a fresh render catches up. We coalesce with a
        100 ms timer: a new step cancels the previous pending render
        and schedules a new one. The render runs at most a few times
        per second while the user is actively dragging.
        """
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

    def _on_waveform_motion(self, event):
        """Track the cursor's horizontal position over the image.

        Used for cursor-anchored zoom: when the user clicks +/-,
        the time under the cursor stays at the same pixel. Stores
        the cursor as a fraction [0.0, 1.0] of the image width so
        the math is independent of the image's actual width.
        """
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
        # Debounce the tooltip update via after_idle. Calling
        # place() on every motion event can leave a trail of
        # "ghost" tooltips at previous cursor positions when the
        # user moves the mouse fast — tkinter's place() can't
        # always keep up at motion-event rate. We instead hide
        # the tooltip immediately and reshow it at the *latest*
        # position once the event queue drains. Storing the event
        # (not just the coords) keeps the original semantics for
        # any caller that might inspect the timestamp.
        self._waveform_last_motion_event = event
        if self._waveform_tooltip_after_id is not None:
            try:
                self.after_cancel(self._waveform_tooltip_after_id)
            except Exception:
                pass
            self._waveform_tooltip_after_id = None
        self._hide_waveform_tooltip()
        self._waveform_tooltip_after_id = self.after_idle(self._show_waveform_tooltip_on_idle)

    def _on_waveform_leave(self, _event):
        """Forget the cursor position when it leaves the image so
        subsequent zoom falls back to the view center."""
        self._waveform_cursor_known = False
        # Cancel any pending tooltip update so a stale ``event`` from
        # before the leave doesn't get a tooltip painted after the
        # cursor has already left the image.
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

    def _update_waveform_tooltip(self, event):
        """Show a tooltip with time + dB at the cursor's plot position.

        No-op when the cursor is over the left dB-axis strip, when the
        popup has been destroyed, or when peaks/duration aren't loaded.
        """
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
        # Cursor time within the visible window.
        t = self._waveform_view_start + (plot_x / plot_w) * view_duration
        # Look up the peak at that time. Peaks are uniformly distributed
        # over [0, _waveform_duration]; the index maps directly.
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
        # Position the tooltip near the cursor, in popup-relative coords.
        # event.x_root/.y_root are screen coords; winfo_rootx() gives
        # the popup's screen origin.
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

    def _on_waveform_wheel(self, event):
        """Mouse wheel over the waveform: zoom by default, pan with Ctrl.

        On Windows/macOS Tk fires ``<MouseWheel>`` with ``event.delta``
        positive for scroll up (zoom in / pan right) and negative for
        scroll down (zoom out / pan left). On Linux Tk fires
        ``<Button-4>`` (up) and ``<Button-5>`` (down) instead. Per-notch
        zoom factor (0.8 / 1.25) is gentler than the +/- buttons
        (0.5 / 2.0); per-notch pan is 0.25 of the view, matching the
        < / > buttons."""
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

    def _on_waveform_drag_start(self, event):
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

    def _on_waveform_drag_motion(self, event):
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
        # delta_x in image-pixel units; the press was at
        # ``_waveform_drag_press_x``, the current cursor is at
        # ``event.x``. Dividing by ``plot_w`` converts to a fraction
        # of the visible window, then multiplied by view_duration
        # gives the time offset to apply (negated so the content
        # follows the cursor).
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

    def _on_waveform_drag_end(self, _event):
        """Release the drag. A click without movement is a no-op
        (the state was set in start but never acted on in motion)."""
        self._waveform_dragging = False

    def _waveform_zoom_in(self):
        self._waveform_zoom_by(0.5)

    def _waveform_zoom_out(self):
        self._waveform_zoom_by(2.0)

    def _waveform_zoom_reset(self):
        """Reset to the full timeline (no zoom)."""
        duration = self._waveform_duration
        if duration <= 0:
            return
        if self._waveform_view_start == 0.0 and self._waveform_view_end == duration:
            return
        self._waveform_view_start = 0.0
        self._waveform_view_end = duration
        self._apply_view()

    def _waveform_zoom_by(self, factor: float):
        """Zoom by a multiplicative factor (< 1 = in, > 1 = out)
        anchored on the cursor's last known position (or view center
        if the cursor hasn't been over the image yet). Clamps the new
        view to [0, duration]."""
        new_start, new_end = self._compute_zoom_view(
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

    def _waveform_pan(self, frac: float):
        """Pan the view by `frac` of the current view duration
        (positive = right, negative = left). Clamps to [0, duration]."""
        new_start, new_end = self._compute_pan_view(
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

    @staticmethod
    def _compute_zoom_view(
        duration: float,
        view_start: float,
        view_end: float,
        cursor_frac: float,
        cursor_known: bool,
        factor: float,
    ) -> tuple[float, float]:
        """Pure view math: zoom by ``factor`` (< 1 in, > 1 out) anchored
        on the cursor or view center. Returns ``(new_start, new_end)``
        clamped to ``[0, duration]`` with ``new_duration`` in
        ``[0.5, duration]``. Identity (no change) if the requested
        factor would not change the duration.
        """
        if duration <= 0:
            return (0.0, 0.0)
        view_duration = view_end - view_start
        new_duration = view_duration * factor
        new_duration = max(0.5, min(duration, new_duration))
        if new_duration == view_duration:
            return (view_start, view_end)
        if cursor_known:
            anchor = view_start + cursor_frac * view_duration
        else:
            anchor = (view_start + view_end) / 2.0
        new_start = anchor - cursor_frac * new_duration
        new_start = max(0.0, min(duration - new_duration, new_start))
        return (new_start, new_start + new_duration)

    @staticmethod
    def _compute_pan_view(
        duration: float,
        view_start: float,
        view_end: float,
        frac: float,
    ) -> tuple[float, float]:
        """Pure view math: shift view by ``frac * view_duration``
        (positive = right, negative = left). Returns ``(new_start,
        new_end)`` clamped to ``[0, duration]``. Identity if the
        current view is the full timeline (no room to pan)."""
        if duration <= 0:
            return (0.0, 0.0)
        view_duration = view_end - view_start
        if view_duration >= duration:
            return (view_start, view_end)
        shift = view_duration * frac
        new_start = view_start + shift
        new_start = max(0.0, min(duration - view_duration, new_start))
        return (new_start, new_start + view_duration)

    def _waveform_pan_left(self):
        self._waveform_pan(-0.25)

    def _waveform_pan_right(self):
        self._waveform_pan(0.25)

    def _on_waveform_slider(self, value: float):
        """Slider drag: jump to the given left-edge time.

        The slider's range is [0, duration], but we clamp the value
        to [0, duration - view_duration] so the view never extends
        past the end. `command` fires on every value change while
        dragging — the render is debounced by the render token
        mechanism (the user will see a quick re-render on the
        final settle; intermediate frames may stutter on slow
        machines, which is acceptable for a preview).
        """
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

    def _update_waveform_controls(self):
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
            # The threshold drives a line in the waveform preview; re-render
            # via a debounced timer so a slider drag does not pile up
            # render calls. Other sliders (min_silence, margin) only
            # affect future pipeline runs, so they don't trigger a
            # re-render here.
            if k == "threshold":
                self._schedule_waveform_threshold_re_render()

        def on_entry_confirm(event=None, sv=slider, mn=min_v, mx=max_v, k=key):
            try:
                val = float(entry_val.get().replace(",", "."))
                val = max(mn, min(mx, val))
                val = round(val, 1)
                sv.set(val)
                self.config[k] = val
                entry_val.delete(0, "end")
                entry_val.insert(0, f"{val:.1f}")
                if k == "threshold":
                    self._schedule_waveform_threshold_re_render()
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

    def _can_preview_waveform(self) -> bool:
        """True iff the input field points at a readable local file.

        Delegates the URL-vs-local-file check to
        ``gui_platform.is_previewable_input`` so the logic is unit-
        testable without a Tk main loop.
        """
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
                self._tk_after(0, lambda: self._log(f"  {enc}: {'[OK]' if ok else 'NO'}"))
            except FileNotFoundError:
                self._tk_after(0, lambda: self._log(f"  {enc}: ffmpeg not found in PATH"))
            except Exception as e:
                self._tk_after(0, lambda e=e: self._log(f"  {enc}: ERROR ({e})"))
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
        self.lbl_size.configure(text=f"Size: {fmt_size(size)}")
        self.lbl_duration.configure(text="Duration: ...")

        def _get_dur():
            dur = get_video_duration(path)
            if dur:
                self.after(
                    0,
                    lambda d=dur: self.lbl_duration.configure(text=f"Duration: {fmt_time(d)}"),
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

        # Read controls (in main thread — Tk widget reads are unsafe from
        # worker threads; the worker receives the values as args, see
        # _pipeline_worker's signature and P1.10 in the fix plan).
        input_raw = self.entry_input.get().strip()
        output_dir = Path(self.entry_output.get().strip() or "./compressed_videos")
        output_dir = output_dir.resolve()
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        video_quality = self.combo_video_quality.get()
        audio_quality = self.combo_audio_quality.get()
        download_quality = self.combo_download_quality.get()
        force = bool(self.chk_force.get())
        per_video_dir = bool(self.chk_per_video_dir.get())
        delete_after = bool(self.chk_delete.get())

        self._ui_update_output(output_dir)

        self._log(
            f"Starting pipeline: input={input_raw}, output_dir={output_dir}, "
            f"method={method}, encoder={encoder}, "
            f"video_quality={video_quality}, download_quality={download_quality}, "
            f"force={force}, "
            f"threshold={self.config['threshold']}, "
            f"min_silence={self.config['min_silence']}, "
            f"margin={self.config['margin']}, "
            f"delete_after={delete_after}, "
            f"per_video_dir={per_video_dir}"
        )

        threading.Thread(
            target=self._pipeline_worker,
            args=(
                input_raw,
                output_dir,
                method,
                encoder,
                video_quality,
                audio_quality,
                download_quality,
                force,
                per_video_dir,
                delete_after,
            ),
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
            self._tk_after(0, lambda: self.lbl_overall.configure(text=""))
            self._tk_after(0, lambda: self.lbl_total.configure(text=""))

    def _pipeline_worker(
        self,
        input_raw: str,
        output_dir: Path,
        method: str,
        encoder: str,
        video_quality: str,
        audio_quality: str,
        download_quality: str,
        force: bool,
        per_video_dir: bool = False,
        delete_after: bool = False,
    ):
        """Worker thread: delegates to PipelineController.run().

        The orchestration (download → silence → cut+concat) lives in
        :class:`stream2video.pipeline_controller.PipelineController` so
        it can be unit-tested without Tk. This method's job is to:
          1. Build PipelineConfig from the widget snapshot (P1.10).
          2. Build PipelineCallbacks whose callables dispatch to the
             Tk main loop via ``self._tk_after``.
          3. Wire the on_live_segment callback for the waveform popup.
          4. Call ``controller.run()`` and map errors to status lines.
          5. Handle the success summary / popup + cleanup.
        """
        from stream2video.pipeline_controller import (
            PipelineCallbacks,
            PipelineCancelled,
            PipelineConcatError,
            PipelineConfig,
            PipelineController,
            PipelineDownloadError,
            PipelineSilenceError,
            PipelineUnexpectedError,
        )

        # Build the immutable config snapshot. All widget reads happen
        # HERE in the main thread (the caller _start_pipeline already
        # snapshoted the widgets into args); the worker only reads these
        # local copies. See P1.10 in the fix plan.
        cfg = PipelineConfig(
            input_raw=input_raw,
            output_dir=output_dir,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            download_quality=download_quality,
            software_fallback=self.config.get("software_fallback", "ask"),
            x264_preset=self.config.get("x264_preset", "medium"),
            encoder_threads=self.config.get("encoder_threads", "auto"),
            output_fps=self.config.get("output_fps", "source"),
            force=force,
            delete_after=delete_after,
            per_video_dir=per_video_dir,
            threshold=float(self.config["threshold"]),
            min_silence=float(self.config["min_silence"]),
            margin=float(self.config["margin"]),
            memory_limit_mb=self.config.get("memory_limit_mb", "auto"),
            memory_reserve_mb=self.config.get("memory_reserve_mb", 2048),
            download_timeout=self.config.get("download_timeout", 28800),
            connect_timeout=self.config.get("connect_timeout", 300),
            no_progress_timeout=self.config.get("no_progress_timeout", 1800),
        )

        # Download progress callback — maps yt-dlp's progress to the
        # overall bar (0..5%) and formats the status string with
        # percent + size + speed + ETA. Runs from yt-dlp's stdout drain
        # thread; all UI writes dispatch to Tk via self._tk_after.
        download_start = time.monotonic()

        def _on_download_progress(p: DownloadProgress) -> None:
            elapsed = time.monotonic() - download_start
            if p.total_bytes and p.total_bytes > 0:
                frac = min(1.0, (p.downloaded_bytes or 0.0) / p.total_bytes)
                self._ui_progress(0.05 * frac)
            else:
                self._ui_progress(min(0.04, 0.005 * elapsed))
            pct = 100.0 * (p.downloaded_bytes or 0.0) / p.total_bytes if p.total_bytes else 0.0
            self._ui_status(
                f"Step 1/3: Downloading {pct:.0f}% "
                f"({fmt_size(int(p.downloaded_bytes or 0))}/{fmt_size(int(p.total_bytes or 0))}) "
                f"at {fmt_speed(p.speed)} ETA {fmt_time(p.eta) if p.eta else '?'}",
                force=True,
            )
            self._ui_overall(elapsed, p.eta or 0.0, True)

        # Live-segment callback for the waveform popup.
        def _on_live_segment(seg_list: list[SilenceSegment]) -> None:
            with self._live_segments_lock:
                self._live_segments[video_path_ref[0]] = list(seg_list)

        # Mid-pipeline hook: after download resolves the output dir,
        # update recent projects + file info panel + output label.
        video_path_ref: list[Path] = [Path()]  # filled by on_output_resolved

        def _on_output_resolved(out_dir: Path, vpath: Path, is_dl: bool) -> None:
            video_path_ref[0] = vpath
            self._add_to_recent_projects(out_dir)
            self._ui_update_output(out_dir)
            self._ui_update_file_info(vpath)
            self._log(f"Project directory: {out_dir}")
            self.after(
                0,
                lambda: self.lbl_encoder.configure(text=f"Encoder: {encoder} ({video_quality})"),
            )

        # Pipeline-complete callback: build summary + show popup.
        def _on_pipeline_complete(summary: dict) -> None:
            from stream2video.gui_helpers import build_completion_summary

            result = build_completion_summary(
                src_size_bytes=summary["src_size_bytes"],
                src_duration=summary["src_duration"],
                dst_size_bytes=summary["dst_size_bytes"],
                dst_duration=summary["keep_duration"],
                pipeline_seconds=summary["pipeline_seconds"],
                output_path=summary["output_path"],
            )
            self._ui_status(result["status"], force=True)
            for line in result["log_lines"]:
                self._log(line)
            self._tk_after(0, lambda: self.lbl_overall.configure(text=""))
            self._ui_total(summary["pipeline_seconds"])
            self._tk_after(0, lambda: messagebox.showinfo("Complete", result["popup"]))

        cb = PipelineCallbacks(
            on_progress=self._ui_progress,
            on_status=self._ui_status,
            on_log=self._log,
            on_info=self._ui_info,
            on_overall=self._ui_overall,
            on_total=self._ui_total,
            on_download_progress=_on_download_progress,
            on_pipeline_complete=_on_pipeline_complete,
        )

        controller = PipelineController(
            cfg=cfg,
            cb=cb,
            cancel_event=self._cancel_event,
            on_live_segment=_on_live_segment,
            on_output_resolved=_on_output_resolved,
        )

        self._pipeline_start = time.monotonic()
        try:
            controller.run()
            # On success, clean up live segments and delete source if
            # requested. The controller's _finish already handled the
            # completion callback; we just do the GUI-specific cleanup.
            if video_path_ref[0]:
                with self._live_segments_lock:
                    self._live_segments.pop(video_path_ref[0], None)
            if delete_after and controller._download_path is not None:
                try:
                    controller._download_path.unlink()
                    self._log(f"Deleted source: {controller._download_path}")
                except OSError as e:
                    self._log(f"[WARN] Could not delete source: {e}")
        except PipelineCancelled:
            self._log("Pipeline cancelled")
            self._ui_status("Cancelled", force=True)
        except PipelineDownloadError as e:
            self._log(f"[ERROR] Download failed: {e}")
            self._ui_status(f"Failed: {e}", force=True)
        except PipelineSilenceError as e:
            self._log(f"[ERROR] Silence detection failed: {e}")
            self._ui_status(f"Failed: {e}", force=True)
        except PipelineConcatError as e:
            self._log(f"[ERROR] {e}")
            self._ui_status(f"Failed: {e}", force=True)
        except PipelineUnexpectedError as e:
            self._log(f"[ERROR] Unexpected: {e}")
            logger.exception("Pipeline error")
            self._ui_status(f"Error: {e}", force=True)
        finally:
            self._output_path = None
            self._download_path = None
            self._tk_after(0, lambda: self._set_running(False))

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
                text=truncate_recent_name(display, RECENT_NAME_MAX),
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

        Called from the pipeline worker thread — the settings write and
        the recent-projects panel re-render are scheduled on the Tk
        main loop via ``_tk_after`` so we never touch Tk widgets or do
        file I/O cross-thread. The config dict (plain Python dict) is
        mutated in-place under the GIL; the only concurrent reader is
        the same worker thread and the Tk main thread's read-only access
        in ``_render_recent_projects`` (which runs after the worker's
        write, on the main thread).
        """
        if not project_path:
            return
        path_str = str(project_path)
        self.config["recent_projects"] = add_recent_project(
            self.config.get("recent_projects", []),
            path_str,
        )
        # Schedule widget work + settings save on the Tk main loop. The
        # worker thread is not allowed to create widgets or touch the
        # root Tk object directly — Tcl wires events through a single
        # thread, so cross-thread widget creation raises or races.
        self._tk_after(0, self._render_recent_projects)

        def _persist():
            try:
                self._save_settings()
            except Exception as e:
                logger.warning("Failed to save settings after adding recent project: %s", e)

        self._tk_after(0, _persist)

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
        """Delegates to gui_platform.dir_size_mb (pure, testable)."""
        return dir_size_mb(path)

    def _open_in_explorer(self, path_str: str):
        """Open the project directory in the platform's file manager.

        Delegates the OS-level call to ``gui_platform.open_in_file_manager``
        so the cross-platform logic (Windows ``os.startfile`` / macOS
        ``open`` / Linux ``xdg-open``) can be unit-tested without a Tk
        main loop. The GUI owns the messagebox + recent-projects prune
        path because those touch widgets; ``FileNotFoundError`` from
        the helper is the signal to prune.
        """
        path = Path(path_str)
        try:
            open_in_file_manager(path)
        except FileNotFoundError:
            messagebox.showwarning(
                "Folder not found",
                f"Directory no longer exists:\n{path_str}",
            )
            self.config["recent_projects"] = [
                p for p in self.config.get("recent_projects", []) if p != path_str
            ]
            self._render_recent_projects()
        except OSError as e:
            self._log(f"[ERROR] Could not open {path}: {e}")

    # ── UI Helpers ───────────────────────────────────────────────

    def _tk_after(self, ms: int, func: Callable[..., Any]) -> None:
        """Schedule ``func`` on the Tk main loop, swallowing ``TclError``
        if the window has been destroyed. Worker threads call UI helpers
        (``_ui_progress``, ``_ui_status``, ``_download_cb``'s helpers…)
        while a pipeline is running; if the user closes the window mid-run
        the queued callbacks would otherwise raise ``TclError`` against a
        destroyed root and propagate up the worker thread, surfacing only
        as an uncaught-exception log line. Treat "window gone" as a quiet
        no-op so the cancel/cleanup path runs cleanly.
        """
        try:
            self.after(ms, func)
        except TclError:
            # Root destroyed — drop the queued update.
            pass

    def _ui_progress(self, value: float):
        self._tk_after(0, lambda: self.progress.set(max(0.0, min(1.0, value))))

    def _ui_overall(self, phase_elapsed: float, phase_remaining: float | None, more_phases: bool):
        """Update the live Elapsed/Remaining line in bottom_frame (the
        label sitting immediately to the right of the progress bar) and
        the Total wall-clock label below the bar.

        'phase_remaining' is the ETA for the CURRENT phase; if more
        phases follow, we append ' + ?' to make clear that the other
        phases' durations are unknown. During phase 1 (no progress
        callback) the label is updated with just elapsed (remaining='?').
        ``phase_remaining=None`` is treated as "unknown" (e.g. before the
        first ffmpeg progress value arrives) and rendered as '?'.
        """
        if self._pipeline_start is None:
            return
        total_elapsed = time.monotonic() - self._pipeline_start
        # ``build_eta_tail`` and ``build_overall_line`` are pure helpers
        # extracted from gui.py so the formatting can be unit-tested
        # without a running Tk main loop. See gui_helpers.py.
        tail = build_eta_tail(phase_remaining, more_phases)
        text = build_overall_line(total_elapsed, tail)
        self._tk_after(0, lambda: self.lbl_overall.configure(text=text))
        self._ui_total(total_elapsed)

    def _ui_total(self, total_elapsed: float):
        """Update the Total wall-clock label below the progress bar."""
        self._tk_after(
            0,
            lambda: self.lbl_total.configure(text=fmt_total_label(total_elapsed)),
        )

    _STATUS_MAX = STATUS_MAX

    def _ui_status(self, text: str, force: bool = False):
        now = time.monotonic()
        # ``should_update_status`` is a pure helper extracted from gui.py
        # so the throttle logic can be unit-tested. See gui_helpers.py
        # and P2.x in the fix plan.
        if not should_update_status(self._last_status_update, now, force=force):
            return
        self._last_status_update = now
        text = truncate_status(text, self._STATUS_MAX)
        self._tk_after(0, lambda: self.lbl_status.configure(text=text))

    def _ui_info(self, text: str):
        self._tk_after(0, lambda t=text: self.lbl_silence.configure(text=t))

    def _ui_update_file_info(self, path: Path):
        self._tk_after(0, lambda: self._update_file_info(path))

    def _ui_update_output(self, path: Path):
        self._tk_after(0, lambda p=path: self.lbl_output.configure(text=f"Output: {p}"))

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
        except TclError:
            # The textbox (or the root window) has been destroyed.
            # Rescheduling would just re-raise forever; stop the poller
            # quietly so close cleans up.
            return
        except Exception as e:
            logger.exception("Log queue poller crashed: %s", e)
        self.after(100, self._poll_log_queue)

    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("stream2video").addHandler(handler)

    # ── Waveform tab ────────────────────────────────────────────

    def _render_waveform_preview(self):
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

        def _run():
            try:
                # Phase 1: read peaks directly from ffmpeg pipe (no WAV).
                # Store them and the duration in self so subsequent
                # renders (overlay, zoom/pan, live poller) can use
                # the same data without re-reading.
                self._tk_after(0, lambda: self._safe_status_set("Loading..."))
                peaks, duration = read_peaks_from_stream(in_path, target_buckets=800)
                if token != self._waveform_render_token:
                    return
                if not peaks or duration <= 0:
                    self.after(
                        0,
                        lambda: self._safe_status_set("No audio stream found"),
                    )
                    self._log("  Waveform preview: no audio in source")
                    return

                # Commit the audio to state. The view is reset to the
                # full timeline — any prior zoom/pan from an earlier
                # render is discarded (the user opened a new preview).
                self._waveform_peaks = peaks
                self._waveform_duration = duration
                self._waveform_video_name = in_path.name
                self._waveform_video_path = in_path
                self._waveform_view_start = 0.0
                self._waveform_view_end = duration
                self._waveform_cursor_frac = 0.5
                self._waveform_cursor_known = False

                # Phase 1.5: render the bare waveform (no overlay yet)
                # so the user gets visual feedback before silence
                # detection finishes. The status line will show
                # "detecting silence..." so the user knows we're working.
                self.after(
                    0,
                    lambda: self._safe_status_set("Rendering peaks... (detecting silence)"),
                )
                self._tk_after(0, lambda: self._apply_view([]))
                if token != self._waveform_render_token:
                    return

                # Phase 2: pull silence segments. Prefer the in-memory
                # live store (the pipeline's on_segment callback keeps it
                # in sync as new segments are detected — no disk I/O).
                # Fall back to the final cache on disk if the live store
                # is empty (e.g. popup opened after the pipeline finished).
                margin = float(config["margin"])
                self._waveform_margin = margin
                # Capture the resolved output dir so the post-pipeline
                # poller (_poll_live_segments) can re-read the final
                # cache from disk after the live store is dropped.
                self._waveform_output_dir = out_dir
                live_segs = self._take_live_snapshot(in_path)
                cached_segs = load_silence_cache(in_path, out_dir, config)
                raw_segments = live_segs if live_segs is not None else cached_segs
                if raw_segments is None:
                    cache_path = out_dir / f"{in_path.stem}_silence_cache.json"
                    # P1.16: dry-run detection. Previously the waveform
                    # popup showed "No silence cache — run the pipeline
                    # first" and skipped detect entirely, leaving the
                    # overlay empty even though the user just wanted to
                    # preview what the current threshold/min_silence
                    # would do. Run ``detect_silence_stream`` (a
                    # lightweight ffmpeg invocation on the source file,
                    # no WAV extract, no cache writes) so the overlay
                    # appears immediately. The result is NOT written
                    # to the final cache — the user must still run the
                    # pipeline to commit the cut plan.
                    self.after(
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
                    # ``detect_silence_stream`` doesn't apply margin
                    # (returns raw segments); apply it here so the
                    # overlay matches what the pipeline would cut.
                    try:
                        raw_dry = detect_silence_stream(
                            in_path,
                            threshold=float(config["threshold"]),
                            min_silence=float(config["min_silence"]),
                        )
                    except SilenceDetectionError as e:
                        logger.warning(f"Dry-run detect failed: {e}")
                        raw_dry = []
                    raw_segments = apply_margin(raw_dry, margin)
                    self._log(
                        f"  Dry-run detected {len(raw_segments)} silence segments "
                        f"(not cached — run the pipeline to commit)"
                    )
                # Apply margin so the overlay matches cut_and_concat.
                # The live store holds raw (pre-margin) segments during
                # detect; the cache holds the canonical margin-applied
                # list — only apply margin for the live (raw) source.
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
                # arrive in the in-memory store. Stops when the pipeline
                # finishes (self.running flips to False) or the popup
                # is closed / re-rendered.
                if self.running:
                    poll_state = {
                        "last_count": len(segments),
                        "last_view": (self._waveform_view_start, self._waveform_view_end),
                    }
                    self.after(
                        1000,
                        lambda: self._poll_live_segments(in_path, margin, token, poll_state),
                    )
            except Exception as e:
                logger.exception("Waveform render failed")
                self._tk_after(0, lambda err=e: self._safe_status_set(f"Error: {err}"))
                self._log(f"[ERROR] Waveform render failed: {e}")
            finally:
                self._waveform_running = False

        threading.Thread(target=_run, daemon=True).start()

    def _take_live_snapshot(self, video_path: Path) -> list[SilenceSegment] | None:
        """Return a copy of the current live segments for `video_path`,
        or None if the pipeline has never published state for it
        (popup opened before detect started, or for a different video).

        The list copy happens inside the lock — a contributor swapping a
        future in-place mutation (e.g. `.extend(...)`) would otherwise
        race the iteration here. Reads under lock are cheap (single dict
        lookup + shallow copy) so this is preferable to a free-standing
        copy after the lock release.
        """
        with self._live_segments_lock:
            segs = self._live_segments.get(video_path)
            return list(segs) if segs is not None else None

    def _apply_view(self, segments: list[SilenceSegment] | None = None):
        """Render the waveform for the current view (view_start → view_end)
        and apply it to the image label. No-op if the popup is closed or
        the audio hasn't been loaded yet.

        Used by every render path:
          - Initial render (Phase 1.5 bare, Phase 3 with overlay)
          - Live poller (re-render with new segments)
          - Zoom / pan buttons and slider (re-render with new view)

        Segments are clipped to the visible window before being passed
        to the renderer — out-of-view silences aren't drawn.
        """
        if self.lbl_wave_image is None or self.lbl_wave_status is None:
            return
        if not self._waveform_peaks or self._waveform_duration <= 0:
            return

        # Honor the latest render token — if a new render started
        # between the schedule and the dispatch, drop this one.
        token = self._waveform_render_token

        view_start = self._waveform_view_start
        view_end = self._waveform_view_end
        view_duration = view_end - view_start
        if view_duration <= 0 or view_duration > self._waveform_duration + 1e-6:
            # Defensive clamp.
            view_start = 0.0
            view_end = self._waveform_duration
            view_duration = view_end - view_start
            self._waveform_view_start = view_start
            self._waveform_view_end = view_end

        # Slice peaks to the visible window — gives higher resolution
        # when zoomed in (more peaks per pixel).
        view_peaks = slice_peaks_by_time(
            self._waveform_peaks, self._waveform_duration, view_start, view_end
        )

        # Clip segments to the view for the overlay. The renderer also
        # clamps out-of-range segments, but doing it here gives an
        # accurate count for the status line and avoids the renderer
        # silently dropping nothing if duration is exactly the boundary.
        #
        # ``segments is None`` means "the caller didn't supply them"
        # (zoom/pan/slider re-render path) — pull from the live store
        # instead, matching what the initial render and the live
        # poller pass in. If we have no path yet (initial render
        # before any peaks) or the live store is empty, fall back to
        # an empty list (no overlay). An explicit ``[]`` is honored
        # (Phase 1.5 bare-waveform render before detect finishes).
        if segments is None and self._waveform_video_path is not None:
            raw = self._take_live_snapshot(self._waveform_video_path)
            if raw is not None:
                segments = apply_margin(raw, self._waveform_margin)
            elif self._waveform_last_segments:
                # Live store miss (e.g., the pipeline keyed it under a
                # resolved path that differs from the user-input path).
                # Fall back to the last segments we successfully rendered
                # so the overlay survives zoom/pan/slider re-renders.
                segments = list(self._waveform_last_segments)
        if segments is None:
            segments = []
        self._waveform_last_segments = segments
        view_segments = [s for s in segments if s.end > view_start and s.start < view_end]

        # Size the rendered image to the popup's image row, with a small
        # fallback for the very first render (window not yet laid out).
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

        # Status line: short form (the title above has the full info).
        self.lbl_wave_status.configure(text=title)

        # Refresh the zoom label / slider to match the new view.
        self._update_waveform_controls()

    def _poll_live_segments(
        self,
        in_path: Path,
        margin: float,
        token: int,
        state: dict,
    ) -> None:
        """Re-read the in-memory live store every second and re-render
        the overlay if the segment count or visible window changed.

        Stops itself when the pipeline finishes (``self.running`` flips
        to False), the render token is invalidated (new render or popup
        closed), or the popup window has been destroyed.
        """
        if token != self._waveform_render_token:
            return
        if not self._wave_window_alive():
            return

        current_view = (self._waveform_view_start, self._waveform_view_end)
        if not self.running:
            # Pipeline finished — the live store has been dropped by
            # _pipeline_worker (post-detect) so we don't double-apply
            # margin, so fall back to the disk cache (margin-applied).
            # The cache may not exist yet if the run was cancelled — in
            # that case we keep the last rendered overlay and stop
            # polling.
            raw = self._take_live_snapshot(in_path)
            if raw is not None:
                # A late on_segment callback arrived AFTER the worker
                # expected the last one but BEFORE the pop took effect
                # — still raw, apply margin as we do mid-run.
                segments = apply_margin(raw, margin)
                if len(segments) != state["last_count"] or current_view != state["last_view"]:
                    self._apply_view(segments)
                    state["last_count"] = len(segments)
                    state["last_view"] = current_view
                    self._log(f"  Pipeline finished — waveform locked at {len(segments)} silences")
            else:
                # Live store miss — re-read the final cache from disk so
                # a popup opened AFTER detect (but before the cache is
                # re-rendered elsewhere) shows the canonical segments
                # rather than the last mid-run snapshot. The render
                # path always sets ``_waveform_output_dir`` first, so
                # ``None`` here means the popup was opened without an
                # underlying render (shouldn't happen, but guard
                # against dereferencing ``None`` — ``load_silence_cache``
                # would treat ``None`` as "no cache path" and raise
                # TypeError before the .exists() check).
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

    # ── Settings Persistence ─────────────────────────────────────

    def _settings_path(self) -> Path:
        return settings_path()

    def _save_settings(self):
        self.config["input_path"] = self.entry_input.get().strip()
        self.config["output_dir"] = self.entry_output.get().strip()
        self.config["method"] = self.combo_method.get()
        self.config["encoder"] = self.combo_encoder.get()
        self.config["video_quality"] = self.combo_video_quality.get()
        self.config["audio_quality"] = self.combo_audio_quality.get()
        self.config["download_quality"] = self.combo_download_quality.get()
        self.config["force"] = bool(self.chk_force.get())
        self.config["delete_after"] = bool(self.chk_delete.get())
        self.config["per_video_dir"] = bool(self.chk_per_video_dir.get())
        self.config["theme"] = self.combo_theme.get()
        self.config["window_geometry"] = self.geometry()
        # P2.6 / Этап 10: JSON write delegated to gui_settings so the
        # serialisation is unit-testable without a Tk main loop.
        try:
            _save_settings_to_disk(self.config)
        except Exception as e:
            self._log(f"[WARN] Could not save settings: {e}")

    def _load_settings(self):
        # P2.6 / Этап 10: JSON read + validation delegated to
        # gui_settings.load_settings so the type guards and the
        # GUI_SESSION_KEYS handling are unit-testable without driving
        # the Tk main loop. Returns a flat dict that the GUI merges
        # into self.config.
        loaded = _load_settings_from_disk()
        for key, value in loaded.items():
            self.config[key] = value

    def _restore_defaults(self):
        self.config = effective_defaults()
        ctk.set_appearance_mode(self.config["theme"])
        self.combo_theme.set(self.config["theme"])
        self.entry_input.delete(0, "end")
        self.entry_output.delete(0, "end")

        self.combo_method.set(self.config["method"])
        self.combo_encoder.set(self.config["encoder"])
        self._on_encoder_change(self.config["encoder"])
        self.combo_video_quality.set(self.config["video_quality"])
        self.combo_audio_quality.set(self.config.get("audio_quality", "medium"))
        self.combo_download_quality.set(self.config["download_quality"])
        self._set_checkbox(self.chk_force, self.config["force"])
        self._set_checkbox(self.chk_delete, self.config["delete_after"])
        self._set_checkbox(self.chk_per_video_dir, self.config["per_video_dir"])
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w, win_h = self._fit_to_screen(sw, sh)
        self.geometry(f"{win_w}x{win_h}")
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

    @staticmethod
    def _fit_to_screen(sw: int, sh: int) -> tuple[int, int]:
        """Delegates to gui_platform.fit_to_screen (pure, testable)."""
        return fit_to_screen(sw, sh)

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
            "video_quality": self.combo_video_quality.get(),
            "audio_quality": self.combo_audio_quality.get(),
            "download_quality": self.combo_download_quality.get(),
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
        video_quality = self.combo_video_quality.get()
        audio_quality = self.combo_audio_quality.get()
        download_quality = self.combo_download_quality.get()
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

        # ``build_cli_command`` is a pure helper extracted from gui.py
        # so the CLI command shape can be unit-tested without
        # instantiating the GUI. See gui_helpers.py and P2.x in the
        # fix plan.
        cmd = build_cli_command(
            inp,
            out_path,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            download_quality=download_quality,
            force=force,
            delete_after=delete_after,
            config_path=config_path,
        )
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
