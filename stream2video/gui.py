"""stream2video GUI — cross-platform desktop application."""

import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from stream2video.download import download, DownloadError
from stream2video.silence import (
    SilenceDetectionError,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)
from stream2video.concat import (
    ConcatError,
    _generate_keep_segments,
    cut_and_concat,
    _get_video_encoder,
    _get_video_duration,
    _check_encoder,
)

logger = logging.getLogger("stream2video.gui")

CONFIG_DEFAULTS = {
    "threshold": -20,
    "min_silence": 1.0,
    "margin": -0.5,
    "method": "segment",
    "encoder": "libx264",
    "force": False,
    "output_dir": "",
    "theme": "dark",
}


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
        self.minsize(1000, 680)
        self.geometry("1200x750")

        self.running = False
        self.cancelled = False
        self.worker_thread: Optional[threading.Thread] = None
        self.config = CONFIG_DEFAULTS.copy()
        self.log_queue: queue.Queue = queue.Queue()
        self.video_path: Optional[Path] = None
        self.silence_segments = None

        self._load_settings()
        ctk.set_appearance_mode(self.config["theme"])

        self._build_ui()
        self._setup_logging()
        self.after(100, self._poll_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI Build ──────────────────────────────────────────────────

    def _build_ui(self):
        self.tabview = ctk.CTkTabview(self, anchor="nw")
        self.tabview.pack(fill="both", expand=True, padx=6, pady=6)

        self.tab_compress = self.tabview.add("Compress")

        self._build_compress_tab()

    def _build_compress_tab(self):
        self.tab_compress.grid_columnconfigure(0, weight=0, minsize=220)
        self.tab_compress.grid_columnconfigure(1, weight=1, minsize=380)
        self.tab_compress.grid_columnconfigure(2, weight=1, minsize=380)
        self.tab_compress.grid_rowconfigure(0, weight=1)

        # ── Left: Info Panel ──
        info_frame = ctk.CTkFrame(self.tab_compress)
        info_header = ctk.CTkLabel(info_frame, text="Info", anchor="w",
                                    font=ctk.CTkFont(size=13, weight="bold"))
        info_header.pack(fill="x", padx=6, pady=(6, 2))
        info_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=4)

        self.lbl_file = ctk.CTkLabel(info_frame, text="File: —", wraplength=200, justify="left")
        self.lbl_file.pack(anchor="w", fill="x", padx=6, pady=2)

        self.lbl_size = ctk.CTkLabel(info_frame, text="Size: —", wraplength=200, justify="left")
        self.lbl_size.pack(anchor="w", fill="x", padx=6, pady=2)

        self.lbl_duration = ctk.CTkLabel(info_frame, text="Duration: —", wraplength=200, justify="left")
        self.lbl_duration.pack(anchor="w", fill="x", padx=6, pady=2)

        self.lbl_silence = ctk.CTkLabel(info_frame, text="Silence: —", wraplength=200, justify="left")
        self.lbl_silence.pack(anchor="w", fill="x", padx=6, pady=2)

        self.lbl_keep = ctk.CTkLabel(info_frame, text="Keep: —", wraplength=200, justify="left")
        self.lbl_keep.pack(anchor="w", fill="x", padx=6, pady=2)

        self.lbl_encoder = ctk.CTkLabel(info_frame, text="Encoder: —", wraplength=200, justify="left")
        self.lbl_encoder.pack(anchor="w", fill="x", padx=6, pady=2)

        ctk.CTkFrame(info_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=6, pady=8)

        ctk.CTkLabel(info_frame, text="Theme:", anchor="w").pack(fill="x", padx=6, pady=(0, 2))
        self.combo_theme = ctk.CTkComboBox(info_frame, values=["dark", "light", "system"], state="readonly",
                                             command=self._on_theme_change)
        self.combo_theme.set(self.config["theme"])
        self.combo_theme.pack(fill="x", padx=6, pady=(0, 6))

        ctk.CTkButton(info_frame, text="Restore defaults", command=self._restore_defaults).pack(fill="x", padx=6, pady=(0, 2))
        ctk.CTkButton(info_frame, text="Copy CLI command", command=self._copy_cli_command).pack(fill="x", padx=6, pady=(0, 6))

        # ── Center: Controls ──
        ctrl_frame = ctk.CTkFrame(self.tab_compress)
        ctrl_frame.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        ctk.CTkLabel(ctrl_frame, text="Controls", anchor="w",
                      font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=6, pady=(10, 2))
        # Input
        ctk.CTkLabel(ctrl_frame, text="Input Video (Twitch/YouTube URL or local path):", anchor="w").pack(fill="x", padx=6, pady=(2, 2))
        row = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 8))
        self.entry_input = ctk.CTkEntry(row, placeholder_text="video.mp4 or https://...")
        self.entry_input.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Browse", width=80, command=self._browse_input).pack(side="right", padx=(6, 0))

        # Output dir
        ctk.CTkLabel(ctrl_frame, text="Output Directory:", anchor="w").pack(fill="x", padx=6, pady=(0, 2))
        row2 = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        row2.pack(fill="x", padx=6, pady=(0, 10))
        self.entry_output = ctk.CTkEntry(row2, placeholder_text="compressed_videos")

        self.entry_output.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row2, text="Browse", width=80, command=self._browse_output).pack(side="right", padx=(6, 0))

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=6, pady=4)

        # Config
        ctk.CTkLabel(ctrl_frame, text="Silence Detection", anchor="w", font=("", 14, "bold")).pack(fill="x", padx=6, pady=(4, 2))

        self._add_slider(ctrl_frame, "Threshold (dB):", "threshold", -60, -5, self.config["threshold"],
                         tooltip="Audio below this level is considered silence. Lower (-30) removes more noise, higher (-5) only cuts loud pauses.")
        self._add_slider(ctrl_frame, "Min Silence (s):", "min_silence", 0.1, 60, self.config["min_silence"],
                         tooltip="Minimum silence duration to cut (seconds). Longer values prevent choppy edits.")
        self._add_slider(ctrl_frame, "Margin (s):", "margin", -3, 5, self.config["margin"],
                         tooltip="Extra context around cuts. Negative trims more silence, positive keeps a buffer.")

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=6, pady=4)

        # Options
        ctk.CTkLabel(ctrl_frame, text="Options", anchor="w", font=("", 14, "bold")).pack(fill="x", padx=6, pady=(4, 2))

        opt_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        opt_frame.pack(fill="x", padx=6, pady=2)

        ctk.CTkLabel(opt_frame, text="Method:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.combo_method = ctk.CTkComboBox(opt_frame, values=["segment", "batch"], state="readonly")
        self.combo_method.set(self.config["method"])
        self.combo_method.grid(row=0, column=1, sticky="ew", padx=(0, 20))

        ctk.CTkLabel(opt_frame, text="Encoder:").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.combo_encoder = ctk.CTkComboBox(
            opt_frame, values=["h264_nvenc", "h264_amf", "h264_mf", "libx264"], state="readonly",
            command=self._on_encoder_change,
        )
        self.combo_encoder.set(self.config["encoder"])
        self.combo_encoder.grid(row=0, column=3, sticky="ew")

        self.lbl_encoder_desc = ctk.CTkLabel(opt_frame, text="", font=("", 11, "italic"))
        self.lbl_encoder_desc.grid(row=1, column=0, columnspan=5, sticky="w", padx=(0, 6), pady=(2, 0))

        self.btn_test_encoders = ctk.CTkButton(opt_frame, text="Test encoder", width=100,
                                                 command=self._test_encoders)
        self.btn_test_encoders.grid(row=0, column=4, padx=(10, 0))

        opt_frame.grid_columnconfigure(1, weight=1)
        opt_frame.grid_columnconfigure(3, weight=1)
        self._on_encoder_change(self.config["encoder"])

        self.chk_force = ctk.CTkCheckBox(ctrl_frame, text="Force re-detect silence (ignore cache)")
        if self.config.get("force"):
            self.chk_force.select()
        self.chk_force.pack(anchor="w", padx=6, pady=(6, 2))

        ctk.CTkFrame(ctrl_frame, height=2, fg_color=("gray70", "gray30")).pack(fill="x", padx=6, pady=6)

        # Action
        action_frame = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        action_frame.pack(fill="x", padx=6, pady=(0, 10))

        self.btn_start = ctk.CTkButton(action_frame, text="Start", command=self._start_pipeline,
                                        height=40, font=("", 14, "bold"))
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_cancel = ctk.CTkButton(action_frame, text="Cancel", command=self._cancel_pipeline,
                                         state="disabled", fg_color="#d32f2f", hover_color="#b71c1c")
        self.btn_cancel.pack(side="left")

        self.lbl_status = ctk.CTkLabel(action_frame, text="", anchor="w")
        self.lbl_status.pack(side="right", fill="x", expand=True, padx=(10, 0))

        self.progress = ctk.CTkProgressBar(ctrl_frame, mode="determinate")
        self.progress.set(0)
        self.progress.pack(fill="x", padx=6, pady=(0, 10))

        # ── Right: Log Panel ──
        log_frame = ctk.CTkFrame(self.tab_compress)
        log_header = ctk.CTkLabel(log_frame, text="Log", anchor="w",
                                   font=ctk.CTkFont(size=13, weight="bold"))
        log_header.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid(row=0, column=2, sticky="nsew", padx=(4, 0), pady=4)

        self.txt_log = ctk.CTkTextbox(log_frame, wrap="word", state="disabled")
        self.txt_log.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

    def _add_slider(self, parent, label: str, key: str, min_v: float, max_v: float, current: float, tooltip: str = ""):
        """Add a labelled slider row with editable value field and default button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(2, 0))

        lbl = ctk.CTkLabel(row, text=label, width=150, anchor="w")
        lbl.pack(side="left")
        if tooltip:
            _Tooltip(lbl, tooltip)

        var = ctk.DoubleVar(value=current)
        slider = ctk.CTkSlider(row, from_=min_v, to=max_v, number_of_steps=200, variable=var,
                                command=lambda v, k=key: self._on_slider(k, v))
        slider.pack(side="left", fill="x", expand=True, padx=(0, 8))

        entry_val = ctk.CTkEntry(row, width=65, justify="right")
        entry_val.insert(0, f"{current:.1f}")
        entry_val.pack(side="right")

        btn_default = ctk.CTkButton(row, text="D", width=28, height=24, font=("", 10, "bold"),
                                     command=lambda k=key, d=CONFIG_DEFAULTS.get(key, current), sv=slider, ev=entry_val: self._reset_default(k, d, sv, ev))
        btn_default.pack(side="right", padx=(4, 0))

        slider._entry_val = entry_val
        slider._cfg_key = key
        setattr(self, f"_slider_{key}", slider)

        original_cmd = slider.cget("command")
        def on_change(v, k=key, ev=entry_val, oc=original_cmd):
            ev.delete(0, "end")
            ev.insert(0, f"{float(v):.1f}")
            self.config[k] = float(v)
            if oc:
                oc(v)

        def on_entry_confirm(event=None, k=key, sv=slider, mn=min_v, mx=max_v):
            try:
                val = float(entry_val.get().replace(",", "."))
                val = max(mn, min(mx, val))
                sv.set(val)
                self.config[k] = round(val, 1)
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

    def _on_slider(self, key, value):
        self.config[key] = round(float(value), 1)

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
        if getattr(self, "_test_proc", None) and self._test_proc.returncode is None:
            self._log("Test already running")
            return
        self._log(f"Testing encoder: {enc} ...")
        self.btn_test_encoders.configure(state="disabled", text="Testing...")

        def _run():
            self._test_proc = None
            try:
                if enc == "libx264":
                    ok = True
                else:
                    self._test_proc = subprocess.Popen(
                        ["ffmpeg", "-y", "-v", "error",
                         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                         "-c:v", enc, "-frames:v", "1",
                         "-f", "null", "-"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    ok = self._test_proc.wait(timeout=10) == 0
                self._log(f"  {enc}: {'[OK]' if ok else 'NO'}")
            except subprocess.TimeoutExpired:
                self._log(f"  {enc}: TIMEOUT")
            finally:
                if self._test_proc and self._test_proc.returncode is None:
                    self._test_proc.kill()
                    self._test_proc.wait(timeout=3)
                self._test_proc = None
                self.after(0, lambda: self.btn_test_encoders.configure(state="normal", text="Test encoder"))

        threading.Thread(target=_run, daemon=True).start()

    def _update_file_info(self, path: Path):
        if not path.exists():
            return
        self.video_path = path
        self.lbl_file.configure(text=f"File: {path.name}")
        size = path.stat().st_size
        self.lbl_size.configure(text=f"Size: {self._fmt_size(size)}")
        dur = _get_video_duration(path)
        if dur:
            self.lbl_duration.configure(text=f"Duration: {self._fmt_time(dur)}")

    # ── Pipeline ─────────────────────────────────────────────────

    def _start_pipeline(self):
        if self.running:
            return

        self._set_running(True)
        self.cancelled = False
        self.silence_segments = None
        self.progress.set(0)
        self.lbl_status.configure(text="Starting...")

        # Read controls
        input_raw = self.entry_input.get().strip()
        output_dir = Path(self.entry_output.get().strip() or "./compressed_videos")
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        force = bool(self.chk_force.get())

        self.config["output_dir"] = str(output_dir.resolve())
        self.config["method"] = method
        self.config["encoder"] = encoder
        self.config["force"] = force

        self._log(f"Starting pipeline: method={method}, encoder={encoder}, force={force}")

        self.worker_thread = threading.Thread(
            target=self._pipeline_worker,
            args=(input_raw, output_dir, method, encoder, force),
            daemon=True,
        )
        self.worker_thread.start()

    def _cancel_pipeline(self):
        if self.running:
            self.cancelled = True
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
            self._ui_status("Step 1/3: Downloading / resolving video...")
            try:
                video_path = download(input_raw, output_dir)
            except DownloadError as e:
                self._log(f"[ERROR] Download failed: {e}")
                self._ui_status(f"Failed: {e}")
                return

            self._ui_update_file_info(video_path)
            self._log(f"Video: {video_path.name} ({video_path.stat().st_size // 1024 // 1024} MB)")

            if self.cancelled:
                return

            # Step 2: Silence detection
            self._ui_progress(0.05)
            self._ui_status("Step 2/3: Detecting silence...")

            config = {
                "threshold": self.config["threshold"],
                "min_silence": self.config["min_silence"],
                "margin": self.config["margin"],
            }

            cache = None if force else load_silence_cache(video_path, output_dir, config)
            if cache:
                self.silence_segments = cache
                self._log(f"Loaded {len(cache)} silence segments from cache")
            else:
                def silence_prog(f: float):
                    self._ui_progress(0.05 + f * 0.35)
                    self._ui_status(f"Step 2/3: Detecting silence... {f * 100:.0f}%")

                self.silence_segments = detect_silence(
                    video_path, **config, progress_callback=silence_prog,
                )
                save_silence_cache(video_path, self.silence_segments, output_dir, config)
                self._log(f"Detected {len(self.silence_segments)} silence segments")

            if self.cancelled:
                return

            # Update info
            keep = _generate_keep_segments(video_path, self.silence_segments)
            keep_dur = sum(e - s for s, e in keep)
            self._ui_info(f"Silence: {len(self.silence_segments)} segments\nKeep: {len(keep)} segments ({self._fmt_time(keep_dur)})")

            # Step 3: Cut & concat
            self._ui_progress(0.4)
            self._ui_status("Step 3/3: Cutting and concatenating...")

            output_path = output_dir / f"{video_path.stem}_compressed.mp4"

            # Check encoder works
            try:
                enc_codec, enc_opts = _get_video_encoder(encoder)
                self._ui_info(f"Encoder: {enc_codec}")
            except Exception:
                self._log("[WARN] Encoder detection failed, will fallback during concat")
                enc_codec = encoder

            def concat_prog(f: float):
                self._ui_progress(0.4 + f * 0.6)
                self._ui_status(f"Step 3/3: Cutting... {f * 100:.0f}%")

            cut_and_concat(
                video_path, self.silence_segments, output_path,
                progress_callback=concat_prog, method=method, encoder=encoder,
            )

            if self.cancelled:
                return

            self._ui_progress(1.0)
            self._ui_status("Complete!")
            self._log(f"[SUCCESS] Output: {output_path} ({output_path.stat().st_size // 1024 // 1024} MB)")

            # Show completion popup
            self.after(0, lambda: messagebox.showinfo(
                "Complete",
                f"Video saved to:\n{output_path}\n\n"
                f"Size: {output_path.stat().st_size // 1024 // 1024} MB\n"
                f"Duration: {self._fmt_time(keep_dur)}"
            ))

        except ConcatError as e:
            self._log(f"[ERROR] {e}")
            self._ui_status(f"Failed: {e}")
        except Exception as e:
            self._log(f"[ERROR] Unexpected: {e}")
            logger.exception("Pipeline error")
            self._ui_status(f"Error: {e}")
        finally:
            self.after(0, lambda: self._set_running(False))

    # ── UI Helpers ───────────────────────────────────────────────

    def _ui_progress(self, value: float):
        self.after(0, lambda: self.progress.set(max(0.0, min(1.0, value))))

    def _ui_status(self, text: str):
        self.after(0, lambda: self.lbl_status.configure(text=text[:80]))

    def _ui_info(self, text: str):
        self.after(0, lambda: self.lbl_silence.configure(text=text))
        if "\n" in text:
            for part in text.split("\n"):
                if part.startswith("Encoder:"):
                    self.after(0, lambda p=part: self.lbl_encoder.configure(text=p))

    def _ui_update_file_info(self, path: Path):
        self.after(0, lambda: self._update_file_info(path))

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _poll_log_queue(self):
        """Periodically drain the log queue into the textbox."""
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.after(100, self._poll_log_queue)

    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("stream2video").addHandler(handler)

    # ── Utilities ────────────────────────────────────────────────

    @staticmethod
    def _fmt_size(bytez: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if bytez < 1024:
                return f"{bytez:.1f} {unit}"
            bytez /= 1024
        return f"{bytez:.1f} TB"

    @staticmethod
    def _fmt_time(secs: float) -> str:
        h, r = divmod(int(secs), 3600)
        m, s = divmod(r, 60)
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
        self.config["theme"] = self.combo_theme.get()
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
            except Exception:
                pass

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
        for key in ("threshold", "min_silence", "margin"):
            slider = getattr(self, f"_slider_{key}", None)
            if slider:
                val = CONFIG_DEFAULTS[key]
                slider.set(val)
                ev = getattr(slider, "_entry_val", None)
                if ev:
                    ev.delete(0, "end")
                    ev.insert(0, f"{val:.1f}")
        self._save_settings()
        self._log("Settings restored to defaults")


    def _copy_cli_command(self):
        parts = ["stream2video"]
        inp = self.entry_input.get().strip()
        out = self.entry_output.get().strip()
        if inp:
            parts.append(inp)
        if out:
            parts.extend(["-o", out])
        parts.extend([
            "--threshold", str(self.config["threshold"]),
            "--min-silence", str(self.config["min_silence"]),
            "--margin", str(self.config["margin"]),
            "--method", self.combo_method.get(),
            "--encoder", self.combo_encoder.get(),
        ])
        if self.chk_force.get():
            parts.append("-f")
        cmd = " ".join(parts)
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self._log("CLI command copied to clipboard: " + cmd)

    def _on_close(self):
        self.cancelled = True
        if getattr(self, "_test_proc", None) and self._test_proc.returncode is None:
            self._test_proc.kill()
        self._save_settings()
        self.destroy()


def main():
    app = Stream2VideoGUI()
    app.mainloop()


if __name__ == "__main__":
    main()


