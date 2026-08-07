"""MainWindowBuildMixin — _build_ui constructor (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: the heavy ``_build_ui`` method that
constructs every widget in the three-column layout (Info / Controls /
Log+Waveform) and the small ``_on_theme_change`` handler bound to the
theme combobox. Must run before any other mixin touches the widgets it
builds.
"""

from __future__ import annotations

from tkinter import StringVar

import customtkinter as ctk

from stream2video.config import (
    DEFAULT_PRESET,
    PRESET_NAMES,
    VALID_DOWNLOAD_QUALITIES,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_QUALITIES,
    VALID_THEMES,
)
from stream2video.gui_widgets import Tooltip as _Tooltip


class MainWindowBuildMixin:
    """One-shot widget construction for the three-column layout."""

    def _on_theme_change(self, choice: str) -> None:
        ctk.set_appearance_mode(choice)
        self.config["theme"] = choice

    def _build_ui(self) -> None:
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
        ctrl_frame = ctk.CTkScrollableFrame(self)
        # Auto-hide scrollbar when content fits (no overflow). CTkScrollableFrame
        # always shows the bar even when yview == (0.0, 1.0); hide it then.
        try:
            _orig_set = ctrl_frame._scrollbar.set

            def _auto_hide_set(first: str | float, last: str | float) -> None:
                _orig_set(first, last)
                try:
                    is_idle = float(first) == 0.0 and float(last) == 1.0
                except Exception:
                    is_idle = False
                try:
                    is_mapped = bool(ctrl_frame._scrollbar.winfo_manager())
                except Exception:
                    is_mapped = True
                if is_idle and is_mapped:
                    ctrl_frame._scrollbar.grid_remove()
                elif not is_idle and not is_mapped:
                    # grid_remove remembers options, so plain grid() restores
                    ctrl_frame._scrollbar.grid()

                # Also suppress xs/croll when idle? keep as-is.
            ctrl_frame._scrollbar.set = _auto_hide_set
            ctrl_frame._parent_canvas.configure(yscrollcommand=_auto_hide_set)
        except Exception:
            pass

        ctk.CTkLabel(
            ctrl_frame, text="Controls", anchor="w", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(fill="x", padx=5, pady=(6, 2))
        # Input
        row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row.pack(fill="x", padx=5, pady=(0, 4))
        ctk.CTkLabel(row, text="Input:", width=58, anchor="w").pack(side="left", padx=(0, 5))
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
        row2 = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row2.pack(fill="x", padx=5, pady=(0, 5))
        ctk.CTkLabel(row2, text="Output:", width=58, anchor="w").pack(side="left", padx=(0, 5))
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
        opt_frame.grid_columnconfigure(1, weight=1)
        opt_frame.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(opt_frame, text="Method:", width=62, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_method = ctk.CTkComboBox(
            opt_frame, values=VALID_METHODS, state="readonly", width=108
        )
        self.combo_method.set(self.config["method"])
        self.combo_method.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(0, 2))
        _Tooltip(
            self.combo_method,
            "Segment: faster, ~1.5h, encodes each segment then joins.\n"
            "Batch: frame-exact, ~6-7h, uses select/aselect filter.\n"
            "Cut-then-encode: best quality, one encode pass after lossless cut.",
        )

        ctk.CTkLabel(opt_frame, text="Encoder:", width=62, anchor="w").grid(
            row=0, column=2, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_encoder = ctk.CTkComboBox(
            opt_frame,
            values=VALID_ENCODERS,
            state="readonly",
            command=self._on_encoder_change,
            width=108,
        )
        self.combo_encoder.set(self.config["encoder"])
        self.combo_encoder.grid(row=0, column=3, sticky="ew", padx=(0, 5), pady=(0, 2))
        _Tooltip(
            self.combo_encoder,
            "h264_nvenc — NVIDIA GPU (GTX 1000+, RTX)\nh264_amf — AMD GPU (RX 400+, Ryzen APU)\nh264_mf — Windows Media Foundation (any GPU)\nlibx264 — CPU software encode (most compatible)",
        )

        self.btn_test_encoders = ctk.CTkButton(
            opt_frame, text="Test encoder", width=90, command=self._test_encoders
        )
        self.btn_test_encoders.grid(row=1, column=3, sticky="e", padx=(3, 0), pady=(0, 2))

        self.lbl_encoder_desc = ctk.CTkLabel(opt_frame, text="", font=("", 10, "italic"))
        self.lbl_encoder_desc.grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(0, 5), pady=(0, 2)
        )

        # Video quality preset — bitrate (HW encoders) / CRF (libx264).
        ctk.CTkLabel(opt_frame, text="Video:", width=62, anchor="w").grid(
            row=2, column=0, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_video_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_QUALITIES, state="readonly", width=108
        )
        self.combo_video_quality.set(self.config["video_quality"])
        self.combo_video_quality.grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=(0, 2))
        _Tooltip(
            self.combo_video_quality,
            "Preset for the encode step.\nsource — encoder defaults\nhigh — 10000k / CRF 18 (large files, best quality)\nmedium — 7000k / CRF 23 (default)\nlow — 3500k / CRF 28 (small files)",
        )

        # Audio quality preset — bitrate of the AAC encode. Kept separate
        # from video_quality so a 192k/256k source is not silently downgraded
        # to 128k (P0.3 in the fix plan).
        ctk.CTkLabel(opt_frame, text="Audio:", width=62, anchor="w").grid(
            row=2, column=2, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_audio_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_QUALITIES, state="readonly", width=108
        )
        self.combo_audio_quality.set(self.config.get("audio_quality", "medium"))
        self.combo_audio_quality.grid(row=2, column=3, sticky="ew", padx=(0, 5), pady=(0, 2))
        _Tooltip(
            self.combo_audio_quality,
            "Audio encode policy.\nsource — codec defaults, native rate/channels\nhigh — 256k (best quality)\nmedium — 192k (default)\nlow — 128k (smaller files)",
        )

        # Download quality preset — Twitch/YouTube resolution cap. Ignored
        # for local files (the source file is used as-is).
        ctk.CTkLabel(opt_frame, text="Download:", width=62, anchor="w").grid(
            row=3, column=0, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_download_quality = ctk.CTkComboBox(
            opt_frame, values=VALID_DOWNLOAD_QUALITIES, state="readonly", width=108
        )
        self.combo_download_quality.set(self.config["download_quality"])
        self.combo_download_quality.grid(row=3, column=1, sticky="ew", padx=(0, 8), pady=(0, 2))
        _Tooltip(
            self.combo_download_quality,
            "Max resolution to download from Twitch/YouTube.\nbest — highest available (default)\n1080p / 720p / 480p / 360p — cap height\nIgnored for local files.",
        )

        # Output format — container/codec choice. ``video`` (default)
        # preserves the historical H.264 + AAC MP4 behaviour; the
        # audio-only values (mp3/opus/aac/wav/flac) drop the video
        # stream entirely and produce a standalone audio file whose
        # codec matches the format's conventional choice.
        ctk.CTkLabel(opt_frame, text="Format:", width=62, anchor="w").grid(
            row=3, column=2, sticky="w", padx=(0, 4), pady=(0, 2)
        )
        self.combo_output_format = ctk.CTkComboBox(
            opt_frame, values=VALID_OUTPUT_FORMATS, state="readonly", width=108
        )
        self.combo_output_format.set(self.config.get("output_format", "video"))
        self.combo_output_format.grid(row=3, column=3, sticky="ew", padx=(0, 5), pady=(0, 2))
        _Tooltip(
            self.combo_output_format,
            "Output container/codec.\nvideo — H.264 + AAC MP4 (default)\nmp3 / opus / aac(m4a) — lossy audio only\nwav / flac — lossless audio only\nAudio-only outputs drop the video stream.",
        )
        self._on_encoder_change(self.config["encoder"])

        toggle_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        toggle_frame.pack(fill="x", padx=5, pady=(2, 1))
        toggle_frame.grid_columnconfigure(0, weight=1)
        toggle_frame.grid_columnconfigure(1, weight=1)

        self.chk_force = ctk.CTkCheckBox(toggle_frame, text="Force re-detect silence")
        if self.config.get("force"):
            self.chk_force.select()
        self.chk_force.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(1, 2))

        self.chk_delete = ctk.CTkCheckBox(toggle_frame, text="Delete downloaded source")
        if self.config.get("delete_after"):
            self.chk_delete.select()
        self.chk_delete.grid(row=0, column=1, sticky="w", padx=(0, 0), pady=(1, 2))

        self.chk_per_video_dir = ctk.CTkCheckBox(toggle_frame, text="Project subdirectory")
        if self.config.get("per_video_dir"):
            self.chk_per_video_dir.select()
        self.chk_per_video_dir.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(1, 2))

        self.chk_completion_sound = ctk.CTkCheckBox(toggle_frame, text="Sound when done")
        if self.config.get("completion_sound", False):
            self.chk_completion_sound.select()
        self.chk_completion_sound.grid(row=1, column=1, sticky="w", padx=(0, 0), pady=(1, 2))
        _Tooltip(
            self.chk_completion_sound,
            "Play a short generated chime when the pipeline finishes. "
            "Success and cancel/failure use different sounds.",
        )

        # Resource preset — bundle of tunables (x264_low_memory,
        # memory_limit_mb, batch_chunk_size, low_process_priority). The
        # combobox sets a baseline; the individual checkboxes/inputs
        # below still win on a per-key basis (their last write to
        # self.config during _collect_gui_state takes precedence over
        # preset application during _start_pipeline).
        preset_row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        preset_row.pack(fill="x", padx=5, pady=(2, 2))
        ctk.CTkLabel(preset_row, text="Resource preset:", width=112, anchor="w").pack(
            side="left", padx=(0, 5)
        )
        self.combo_preset = ctk.CTkComboBox(
            preset_row,
            values=list(PRESET_NAMES),
            state="readonly",
            width=180,
        )
        self.combo_preset.set(self.config.get("preset", DEFAULT_PRESET))
        self.combo_preset.pack(side="left")
        _Tooltip(
            self.combo_preset,
            "Resource preset — a bundle of tunables (x264_low_memory, "
            "memory_limit_mb, batch_chunk_size, low_process_priority) "
            "applied as a baseline before any explicit overrides.\n"
            "  - low_memory: 4-8 GB machines (x264_low_memory=True, "
            "batch_chunk_size=20, low_process_priority=True).\n"
            "  - balanced: historical defaults.\n"
            "  - maximum_performance: trade RAM for throughput "
            "(x264_low_memory=False, memory_limit_mb=0, "
            "batch_chunk_size=80).\n"
            "Explicit checkboxes below still override the preset on "
            "a per-key basis — e.g. pick 'low_memory' then uncheck "
            "'low_process_priority' to keep the other low-memory "
            "tunables but restore normal scheduling priority.",
        )

        advanced_toggle_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        advanced_toggle_frame.pack(fill="x", padx=5, pady=(0, 2))
        advanced_toggle_frame.grid_columnconfigure(0, weight=1)
        advanced_toggle_frame.grid_columnconfigure(1, weight=1)

        self.chk_x264_low_memory = ctk.CTkCheckBox(
            advanced_toggle_frame,
            text="Low-memory x264",
        )
        if self.config.get("x264_low_memory", False):
            self.chk_x264_low_memory.select()
        self.chk_x264_low_memory.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(1, 2))
        _Tooltip(
            self.chk_x264_low_memory,
            "Reduces x264's frame-buffer footprint via rc-lookahead=10, "
            "ref=1, bframes=0. Produces slightly larger files but uses "
            "significantly less RAM during encode. Useful on memory-"
            "constrained machines (4-8 GB).",
        )

        self.chk_use_crf = ctk.CTkCheckBox(
            advanced_toggle_frame,
            text="Use CRF",
        )
        if self.config.get("use_crf", False):
            self.chk_use_crf.select()
        self.chk_use_crf.grid(row=1, column=1, sticky="w", padx=(0, 0), pady=(1, 2))
        _Tooltip(
            self.chk_use_crf,
            "Use quality-fixed video encoding instead of fixed bitrate. "
            "libx264 uses CRF; NVENC/AMF use CQ/QP modes; MF uses quality mode. "
            "Files may be smaller or larger depending on content.",
        )

        self.chk_gapless_concat = ctk.CTkCheckBox(
            advanced_toggle_frame,
            text="Gapless audio concat",
        )
        if self.config.get("gapless_concat", False):
            self.chk_gapless_concat.select()
        self.chk_gapless_concat.grid(row=0, column=1, sticky="w", padx=(0, 0), pady=(1, 2))
        _Tooltip(
            self.chk_gapless_concat,
            "Re-encodes the audio track in the final concat pass so "
            "per-segment AAC priming (~21ms per segment) doesn't "
            "accumulate as A/V drift on multi-segment outputs. Video "
            "is stream-copied (no quality loss); only audio is "
            "re-encoded. Default off (concat demuxer, faster). "
            "Equivalent to cut_then_encode's gapless property but "
            "with frame accuracy.",
        )

        self.chk_low_process_priority = ctk.CTkCheckBox(
            advanced_toggle_frame,
            text="Low process priority",
        )
        if self.config.get("low_process_priority", False):
            self.chk_low_process_priority.select()
        self.chk_low_process_priority.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(1, 2)
        )
        _Tooltip(
            self.chk_low_process_priority,
            "Spawns ffmpeg at a lower scheduling priority so a "
            "long-running encode doesn't starve interactive "
            "applications. On Windows: BELOW_NORMAL_PRIORITY_CLASS; "
            "on Linux/macOS: nice +10. Useful for unattended batch "
            "processing on shared/desktop machines. Default off "
            "(normal priority, faster encoding).",
        )

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(
            fill="x", padx=5, pady=4
        )

        # Action
        action_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        action_frame.pack(fill="x", padx=5, pady=(0, 6))

        # Action row: [Start] [Cancel] only. The Step/Complete status and
        # the progress block moved to the bottom of this column (fixed
        # strip at grid row 1, built below).
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

        # Controls column stretches to the full window height again
        # (the progress block moved to the Log column, so after the
        # action row the column is just empty space inside the scroll
        # area rather than a dead band under a top-pinned widget).
        ctrl_frame.grid(row=0, column=1, sticky="nsew", padx=3, pady=4)

        # ── Right: Log panel + Waveform button ──
        # The waveform preview is opened in its own Toplevel window when
        # the user clicks "Waveform" (see ``_open_waveform_window``). The
        # popup auto-renders the preview on open; no render button.
        right_frame = ctk.CTkFrame(self)
        # The log row is free to stretch (it is the only ``weight=1``
        # row in this column). The progress block below it stays pinned
        # to the bottom via its own grid's ``sticky="sew"``.
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

        # Log: fixed height so the progress block below is always visible
        # (and never collapses when the window is short). Right_frame's
        # row 2 is weight=1 so the log expands with the column but never
        # shrinks below the request.
        self.txt_log = ctk.CTkTextbox(right_frame, wrap="word", state="disabled")
        self.txt_log.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)

        # Progress block: moved from the bottom of the Controls column.
        # Four-row layout pinned to the bottom of the column:
        #   row 0-1: Step status (two lines so Cutting 50% (0:10/0:10) fits)
        #   row 2: thin per-phase bar (fraction of the CURRENT phase)
        #   row 3: overall bar + %
        #   row 4: Elapsed/Remaining | Total (second line under bar, frees space)
        prog_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        prog_frame.grid(row=3, column=0, sticky="sew", padx=4, pady=(0, 4))
        prog_frame.grid_columnconfigure(0, weight=0)  # bar: fixed width
        prog_frame.grid_columnconfigure(1, weight=0)  # percent
        prog_frame.grid_columnconfigure(2, weight=1)  # filler
        prog_frame.grid_columnconfigure(3, weight=0)  # spare

        # Step / Complete status — two lines so Cutting 50% (0:10/0:10) fits
        # without widening the window. lbl_status is line 1, lbl_status2 line 2.
        self.lbl_status = ctk.CTkLabel(prog_frame, text="", anchor="w", width=200)
        self.lbl_status.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 1))
        self.lbl_status2 = ctk.CTkLabel(prog_frame, text="", anchor="w", width=200)
        self.lbl_status2.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 2))

        # Per-phase bar (0..1 within the current phase). Thin so it
        # reads as a sub-bar of the overall progress below it. Updated via
        # ``_set_phase_progress`` which ``PipelineCallbacks.on_phase_progress``
        # buffers into.
        self.phase_progress = ctk.CTkProgressBar(
            prog_frame,
            mode="determinate",
            height=4,
            width=160,
        )
        self.phase_progress.set(0)
        self.phase_progress.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 2))
        _Tooltip(
            self.phase_progress,
            "Current phase progress (0-100% within the active step)",
        )

        # Overall pipeline progress bar.
        self.progress = ctk.CTkProgressBar(
            prog_frame,
            mode="determinate",
            height=10,
            width=160,
        )
        self.progress.set(0)
        self.progress.grid(row=3, column=0, sticky="w", padx=(0, 6))
        # Live tooltip on the bar (updated on every overall tick with the
        # smoothed ETA + total estimate). Initial text is a static hint.
        self.progress_tooltip = _Tooltip(
            self.progress,
            "Overall pipeline progress — hover during a run for ETA details",
        )
        # Cache the theme's default bar colour so
        # ``ProgressUiMixin._set_progress_bar_color(None)`` can restore
        # it at the start of the next run (after a success/failure tint).
        self._default_progress_color = self.progress.cget("progress_color")
        self.lbl_progress_pct = ctk.CTkLabel(
            prog_frame,
            text="0%",
            anchor="w",
            width=42,
            text_color=("gray40", "gray60"),
        )
        self.lbl_progress_pct.grid(row=3, column=1, sticky="w", padx=(0, 6))
        # Reserve two spots on bar row for legacy layout (unused now but keeps grid)
        # Live Elapsed/Remaining for the current phase — now on its own line under bar.
        self.lbl_overall = ctk.CTkLabel(
            prog_frame,
            text="",
            anchor="w",
            text_color=("gray40", "gray60"),
        )
        self.lbl_overall.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        # Total pipeline wall-clock, updated in real time — also on line 2.
        self.lbl_total = ctk.CTkLabel(
            prog_frame,
            text="",
            anchor="e",
            text_color=("gray40", "gray60"),
        )
        self.lbl_total.grid(row=4, column=3, sticky="e")
