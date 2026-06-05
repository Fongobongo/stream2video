"""stream2video GUI — cross-platform desktop application."""

import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from stream2video.config import CONFIG_DEFAULTS
from stream2video.download import download, DownloadCancelledError, DownloadError
from stream2video.silence import (
    detect_silence,
    load_silence_cache,
    save_silence_cache,
    SilenceCancelledError,
    SilenceDetectionError,
)
from stream2video.concat import (
    CancelledError,
    ConcatError,
    check_encoder,
    cut_and_concat,
    generate_keep_segments,
)
from stream2video.utils import get_active_process, get_video_duration

logger = logging.getLogger("stream2video.gui")


class _Tooltip:
    """A hover tooltip for any tkinter/ctk widget."""

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self._tip: Optional[ctk.CTkToplevel] = None
        self._after_id: Optional[str] = None
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
        ctk.CTkLabel(tw, text=self.text, wraplength=320,
                     fg_color=("gray85", "gray15"),
                     text_color=("gray10", "gray90"),
                     corner_radius=4, padx=8, pady=4).pack()
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


class Stream2VideoGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("stream2video")

        self.running = False
        self._cancel_event = threading.Event()
        self._test_running = False
        self.config = CONFIG_DEFAULTS.copy()
        self.log_queue: queue.Queue = queue.Queue()
        self._output_path: Optional[Path] = None
        self._download_path: Optional[Path] = None
        self._last_status_update: float = 0.0

        self._load_settings()
        ctk.set_appearance_mode(self.config["theme"])

        # Fit window to screen if resolution is small
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w = max(1, min(1250, sw - 40))
        win_h = max(1, min(680, sh - 60))
        self.minsize(max(1, min(1130, sw - 40)), max(1, min(620, sh - 60)))

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
        info_header = ctk.CTkLabel(info_frame, text="Info", anchor="w",
                                    font=ctk.CTkFont(size=12, weight="bold"))
        info_header.pack(fill="x", padx=5, pady=(4, 2))
        info_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 3), pady=4)

        self.lbl_file = ctk.CTkLabel(info_frame, text="File: —", wraplength=190, justify="left")
        self.lbl_file.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_output = ctk.CTkLabel(info_frame, text="Output: —", wraplength=190, justify="left")
        self.lbl_output.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_size = ctk.CTkLabel(info_frame, text="Size: —", wraplength=190, justify="left")
        self.lbl_size.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_duration = ctk.CTkLabel(info_frame, text="Duration: —", wraplength=190, justify="left")
        self.lbl_duration.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_silence = ctk.CTkLabel(info_frame, text="Silence: —", wraplength=190, justify="left")
        self.lbl_silence.pack(anchor="w", fill="x", padx=5, pady=1)

        self.lbl_encoder = ctk.CTkLabel(info_frame, text="Encoder: —", wraplength=190, justify="left")
        self.lbl_encoder.pack(anchor="w", fill="x", padx=5, pady=1)

        ctk.CTkFrame(info_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=5, pady=6)

        ctk.CTkLabel(info_frame, text="Theme:", anchor="w").pack(fill="x", padx=5, pady=(0, 1))
        self.combo_theme = ctk.CTkComboBox(info_frame, values=["dark", "light", "system"], state="readonly",
                                             command=self._on_theme_change)
        self.combo_theme.set(self.config["theme"])
        self.combo_theme.pack(fill="x", padx=5, pady=(0, 4))

        ctk.CTkButton(info_frame, text="Restore defaults", command=self._restore_defaults).pack(fill="x", padx=5, pady=(0, 4))
        ctk.CTkButton(info_frame, text="Copy CLI command", command=self._copy_cli_command).pack(fill="x", padx=5, pady=(0, 4))

        # ── Center: Controls ──
        ctrl_frame = ctk.CTkFrame(self)
        ctrl_frame.grid(row=0, column=1, sticky="nsew", padx=3, pady=4)

        ctk.CTkLabel(ctrl_frame, text="Controls", anchor="w",
                      font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=5, pady=(6, 2))
        # Input
        ctk.CTkLabel(ctrl_frame, text="Input Video (Twitch/YouTube URL or local path):", anchor="w").pack(fill="x", padx=5, pady=(1, 1))
        row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row.pack(fill="x", padx=5, pady=(0, 5))
        self.entry_input = ctk.CTkEntry(row, placeholder_text="video.mp4 or https://...")
        self.entry_input.pack(side="left", fill="x", expand=True)
        if self.config.get("input_path"):
            self.entry_input.insert(0, self.config["input_path"])
        ctk.CTkButton(row, text="Browse", width=70, command=self._browse_input).pack(side="right", padx=(5, 0))

        # Output dir
        ctk.CTkLabel(ctrl_frame, text="Output Directory:", anchor="w").pack(fill="x", padx=5, pady=(0, 1))
        row2 = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row2.pack(fill="x", padx=5, pady=(0, 6))
        self.entry_output = ctk.CTkEntry(row2, placeholder_text="compressed_videos")
        self.entry_output.pack(side="left", fill="x", expand=True)
        if self.config.get("output_dir"):
            self.entry_output.insert(0, self.config["output_dir"])
        ctk.CTkButton(row2, text="Browse", width=70, command=self._browse_output).pack(side="right", padx=(5, 0))

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=5, pady=3)

        # Config
        ctk.CTkLabel(ctrl_frame, text="Silence Detection", anchor="w", font=("", 13, "bold")).pack(fill="x", padx=5, pady=(3, 1))

        self._add_slider(ctrl_frame, "Threshold (dB):", "threshold", -60, -5, self.config["threshold"],
                         tooltip="Audio below this level is considered silence. Lower (-30) removes more noise, higher (-5) only cuts loud pauses.")
        self._add_slider(ctrl_frame, "Min Silence (s):", "min_silence", 0.1, 60, self.config["min_silence"],
                         tooltip="Minimum silence duration to cut (seconds). Longer values prevent choppy edits.")
        self._add_slider(ctrl_frame, "Margin (s):", "margin", -3, 5, self.config["margin"],
                         tooltip="How much to shrink silence zones. Positive = shrink silence (keep more audio around phrases). Negative = expand silence (cut more aggressively). 0 = no adjustment.")

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=5, pady=3)

        # Options
        ctk.CTkLabel(ctrl_frame, text="Options", anchor="w", font=("", 13, "bold")).pack(fill="x", padx=5, pady=(3, 1))

        opt_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        opt_frame.pack(fill="x", padx=5, pady=1)

        ctk.CTkLabel(opt_frame, text="Method:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.combo_method = ctk.CTkComboBox(opt_frame, values=["segment", "batch"], state="readonly")
        self.combo_method.set(self.config["method"])
        self.combo_method.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        _Tooltip(self.combo_method, "Segment: faster, ~1.5h, encodes each segment then joins.\nBatch: frame-exact, ~6-7h, uses select/aselect filter.")

        ctk.CTkLabel(opt_frame, text="Encoder:").grid(row=1, column=0, sticky="w", padx=(0, 5))
        self.combo_encoder = ctk.CTkComboBox(
            opt_frame, values=["h264_nvenc", "h264_amf", "h264_mf", "libx264"], state="readonly",
            command=self._on_encoder_change,
        )
        self.combo_encoder.set(self.config["encoder"])
        self.combo_encoder.grid(row=1, column=1, sticky="ew", padx=(0, 5))
        _Tooltip(self.combo_encoder, "h264_nvenc — NVIDIA GPU (GTX 1000+, RTX)\nh264_amf — AMD GPU (RX 400+, Ryzen APU)\nh264_mf — Windows Media Foundation (any GPU)\nlibx264 — CPU software encode (most compatible)")

        self.btn_test_encoders = ctk.CTkButton(opt_frame, text="Test encoder", width=90,
                                                 command=self._test_encoders)
        self.btn_test_encoders.grid(row=1, column=2, padx=(5, 0))

        self.lbl_encoder_desc = ctk.CTkLabel(opt_frame, text="", font=("", 10, "italic"))
        self.lbl_encoder_desc.grid(row=2, column=0, columnspan=4, sticky="w", padx=(0, 5), pady=(1, 0))

        opt_frame.grid_columnconfigure(1, weight=1)
        self._on_encoder_change(self.config["encoder"])

        self.chk_force = ctk.CTkCheckBox(ctrl_frame, text="Force re-detect silence (ignore cache)")
        if self.config.get("force"):
            self.chk_force.select()
        self.chk_force.pack(anchor="w", padx=5, pady=(4, 1))

        self.chk_delete = ctk.CTkCheckBox(ctrl_frame, text="Delete downloaded source after success")
        if self.config.get("delete_after"):
            self.chk_delete.select()
        self.chk_delete.pack(anchor="w", padx=5, pady=(4, 1))

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=5, pady=4)

        # Action
        action_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        action_frame.pack(fill="x", padx=5, pady=(0, 6))

        self.btn_start = ctk.CTkButton(action_frame, text="Start", command=self._start_pipeline,
                                        height=36, font=("", 13, "bold"))
        self.btn_start.pack(side="left", padx=(0, 8))

        self.btn_cancel = ctk.CTkButton(action_frame, text="Cancel", command=self._cancel_pipeline,
                                         state="disabled", fg_color="#d32f2f", hover_color="#b71c1c")
        self.btn_cancel.pack(side="left")

        self.lbl_status = ctk.CTkLabel(action_frame, text="", anchor="w", width=500)
        self.lbl_status.pack(side="right", fill="x", expand=True, padx=(8, 0))

        self.progress = ctk.CTkProgressBar(ctrl_frame, mode="determinate")
        self.progress.set(0)
        self.progress.pack(fill="x", padx=5, pady=(0, 6))

        # ── Right: Log Panel ──
        log_frame = ctk.CTkFrame(self)
        log_header = ctk.CTkLabel(log_frame, text="Log", anchor="w",
                                   font=ctk.CTkFont(size=12, weight="bold"))
        log_header.grid(row=0, column=0, sticky="ew", padx=4, pady=(3, 0))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid(row=0, column=2, sticky="nsew", padx=(3, 4), pady=4)

        self.txt_log = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=4, pady=3)

    def _add_slider(self, parent, label: str, key: str, min_v: float, max_v: float, current: float, tooltip: str = ""):
        """Add a labelled slider row with editable value field and default button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(2, 0))

        lbl = ctk.CTkLabel(row, text=label, width=150, anchor="w")
        lbl.pack(side="left")
        if tooltip:
            _Tooltip(lbl, tooltip)

        slider = ctk.CTkSlider(row, from_=min_v, to=max_v, number_of_steps=round((max_v - min_v) * 10))
        slider.set(current)
        slider.pack(side="left", fill="x", expand=True, padx=(0, 8))

        entry_val = ctk.CTkEntry(row, width=65, justify="right")
        entry_val.insert(0, f"{current:.1f}")
        entry_val.pack(side="right")

        btn_default = ctk.CTkButton(row, text="D", width=28, height=24, font=("", 10, "bold"),
                                     command=lambda k=key, d=CONFIG_DEFAULTS.get(key, current), sv=slider, ev=entry_val: self._reset_default(k, d, sv, ev))
        btn_default.pack(side="right", padx=(4, 0))

        slider._entry_val = entry_val
        setattr(self, f"_slider_{key}", slider)

        def on_change(v, k=key, ev=entry_val):
            ev.delete(0, "end")
            ev.insert(0, f"{float(v):.1f}")
            self.config[k] = round(float(v), 1)

        def on_entry_confirm(event=None, sv=slider, mn=min_v, mx=max_v):
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

    def _reset_default(self, key: str, default: float, slider, entry):
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
            filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.ts"), ("All files", "*.*")]
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

    ENCODER_DESCRIPTIONS = {
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
                self.after(0, lambda: self.btn_test_encoders.configure(
                    state="normal", text="Test encoder"))

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
                self.after(0, lambda d=dur: self.lbl_duration.configure(text=f"Duration: {self._fmt_time(d)}"))
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

        self._ui_update_output(output_dir)

        self._log(
            f"Starting pipeline: input={input_raw}, output_dir={output_dir}, "
            f"method={method}, encoder={encoder}, force={force}, "
            f"threshold={self.config['threshold']}, "
            f"min_silence={self.config['min_silence']}, "
            f"margin={self.config['margin']}, "
            f"delete_after={bool(self.chk_delete.get())}"
        )

        threading.Thread(
            target=self._pipeline_worker,
            args=(input_raw, output_dir, method, encoder, force),
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

    def _pipeline_worker(self, input_raw: str, output_dir: Path, method: str, encoder: str, force: bool):
        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Download / resolve path
            self._ui_progress(0.0)
            self._ui_status("Step 1/3: Downloading / resolving video...", force=True)
            self._log("Phase 1/3: Downloading / resolving video...")

            def dl_prog(frac: float, text: str):
                self._ui_progress(frac * 0.05)
                self._ui_status(f"Step 1/3: Downloading... {text}")

            try:
                download_result = download(
                    input_raw, output_dir,
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

            self._download_path = video_path if download_result.is_downloaded else None
            self._ui_update_file_info(video_path)
            file_size_mb = video_path.stat().st_size // 1024 // 1024
            if download_result.is_downloaded:
                self._log(f"Downloaded: {input_raw} -> {video_path}")
            else:
                self._log(f"Download skipped (file already on disk): {video_path}")
            self._log(f"Size: {file_size_mb} MB")

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
                f"Phase 2/3: Detecting silence "
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
                    eta = elapsed / f - elapsed if f > 0.01 else 0
                    total = elapsed + eta
                    self._ui_progress(0.05 + f * 0.35)
                    self._ui_status(
                        f"Step 2/3: Silence... {f * 100:.0f}% "
                        f"({self._fmt_time(elapsed)}/{self._fmt_time(total)})"
                    )

                silence_segments = detect_silence(
                    video_path, **config,
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
            self._ui_info(f"Silence: {len(silence_segments)} segments\nKeep: {len(keep)} segments ({self._fmt_time(keep_dur)})")

            # Step 3: Cut & concat
            self._ui_progress(0.4)
            self._ui_status("Step 3/3: Cutting and concatenating...", force=True)
            self._log(
                f"Phase 3/3: Cutting & concatenating "
                f"(method={method}, encoder={encoder})..."
            )

            output_path = output_dir / f"{video_path.stem}_compressed.mp4"
            self._output_path = output_path

            self.after(0, lambda: self.lbl_encoder.configure(text=f"Encoder: {encoder}"))

            cut_start = time.monotonic()

            def concat_prog(f: float):
                elapsed = time.monotonic() - cut_start
                eta = elapsed / f - elapsed if f > 0.01 else 0
                total = elapsed + eta
                self._ui_progress(0.4 + f * 0.6)
                self._ui_status(
                    f"Step 3/3: Cutting... {f * 100:.0f}% "
                    f"({self._fmt_time(elapsed)}/{self._fmt_time(total)})"
                )

            cut_and_concat(
                video_path, silence_segments, output_path,
                progress_callback=concat_prog, method=method, encoder=encoder,
                cancel_callback=lambda: self._cancel_event.is_set(),
            )

            self._output_path = None
            self._ui_progress(1.0)
            self._ui_status("Complete!", force=True)
            self._log(
                f"[SUCCESS] Output: {output_path} "
                f"({output_path.stat().st_size // 1024 // 1024} MB)"
            )

            # Delete downloaded source if requested
            if bool(self.chk_delete.get()) and self._download_path is not None:
                try:
                    self._download_path.unlink()
                    self._log(f"Deleted source: {self._download_path}")
                except OSError as e:
                    self._log(f"[WARN] Could not delete source: {e}")
            self._download_path = None

            # Show completion popup
            self.after(0, lambda: messagebox.showinfo(
                "Complete",
                f"Video saved to:\n{output_path}\n\n"
                f"Size: {output_path.stat().st_size // 1024 // 1024} MB\n"
                f"Duration: {self._fmt_time(keep_dur)}"
            ))

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

    # ── UI Helpers ───────────────────────────────────────────────

    def _ui_progress(self, value: float):
        self.after(0, lambda: self.progress.set(max(0.0, min(1.0, value))))

    def _ui_status(self, text: str, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_status_update < 0.5:
            return
        self._last_status_update = now
        self.after(0, lambda: self.lbl_status.configure(text=text[:80]))

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
        self.config["theme"] = self.combo_theme.get()
        self.config["window_geometry"] = self.geometry()
        try:
            with open(self._settings_path(), "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            self._log(f"[WARN] Could not save settings: {e}")

    def _load_settings(self):
        sp = self._settings_path()
        if sp.exists():
            try:
                with open(sp) as f:
                    loaded = json.load(f)
                    self.config.update(loaded)
            except Exception as e:
                logger.warning("Failed to load settings: %s", e)

    def _restore_defaults(self):
        self.config = CONFIG_DEFAULTS.copy()
        ctk.set_appearance_mode(CONFIG_DEFAULTS["theme"])
        self.combo_theme.set(CONFIG_DEFAULTS["theme"])
        self.entry_input.delete(0, "end")
        self.entry_output.delete(0, "end")

        self.combo_method.set(CONFIG_DEFAULTS["method"])
        self.combo_encoder.set(CONFIG_DEFAULTS["encoder"])
        self._on_encoder_change(CONFIG_DEFAULTS["encoder"])
        self.chk_force.deselect()
        self.chk_delete.deselect()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{min(1250, sw - 40)}x{min(680, sh - 60)}")
        for key in ("threshold", "min_silence", "margin"):
            slider = getattr(self, f"_slider_{key}", None)
            if slider:
                val = CONFIG_DEFAULTS[key]
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
