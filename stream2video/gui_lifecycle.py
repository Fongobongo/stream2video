"""LifecycleMixin — settings I/O, restore defaults, on-close (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: ``_save_settings`` (snapshot widgets
→ settings.json), ``_load_settings`` (read settings.json → self.config),
``_restore_defaults``, ``_save_user_defaults``, ``_copy_cli_command``,
``_on_close`` (WM_DELETE_WINDOW handler — cancel running pipeline,
delete incomplete output, save settings, destroy).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from stream2video.config import (
    effective_defaults,
    save_user_defaults,
    settings_path,
    user_defaults_path,
)
from stream2video.gui_helpers import build_cli_command
from stream2video.gui_platform import fit_to_screen
from stream2video.gui_settings import load_settings as _load_settings_from_disk
from stream2video.gui_settings import save_settings as _save_settings_to_disk
from stream2video.settings_io import (
    build_save_settings_snapshot,
    build_user_defaults_snapshot,
    write_cli_config_yaml,
)
from stream2video.slider_widgets import format_slider_entry_value
from stream2video.utils import get_active_process

_logger = logging.getLogger("stream2video.gui")


class LifecycleMixin:
    """Settings persistence + window lifecycle (close / restore / save)."""

    _output_path: Path | None
    _download_path: Path | None

    def _save_settings(self) -> None:
        # Read widgets in the main thread (Tk reads are unsafe from
        # worker threads); forward the snapshot through the pure
        # :func:`stream2video.settings_io.build_save_settings_snapshot`
        # so the field list / key order is unit-tested in isolation.
        widgets = {
            "input_path": self.entry_input.get().strip(),
            "output_dir": self.entry_output.get().strip(),
            "method": self.combo_method.get(),
            "encoder": self.combo_encoder.get(),
            "video_quality": self.combo_video_quality.get(),
            "audio_quality": self.combo_audio_quality.get(),
            "download_quality": self.combo_download_quality.get(),
            "output_format": self.combo_output_format.get(),
            "force": bool(self.chk_force.get()),
            "delete_after": bool(self.chk_delete.get()),
            "per_video_dir": bool(self.chk_per_video_dir.get()),
            "completion_sound": bool(self.chk_completion_sound.get()),
            "x264_low_memory": bool(self.chk_x264_low_memory.get()),
            "gapless_concat": bool(self.chk_gapless_concat.get()),
            "low_process_priority": bool(self.chk_low_process_priority.get()),
            "preset": self.combo_preset.get(),
            "theme": self.combo_theme.get(),
            "window_geometry": self.geometry(),
        }
        self.config.update(build_save_settings_snapshot(widgets))
        try:
            _save_settings_to_disk(self.config)
        except Exception as e:
            self._log(f"[WARN] Could not save settings: {e}")

    def _load_settings(self) -> None:
        loaded = _load_settings_from_disk()
        for key, value in loaded.items():
            self.config[key] = value

    def _restore_defaults(self) -> None:
        self.config = effective_defaults()
        ctk.set_appearance_mode(self.config["theme"])
        self.combo_theme.set(self.config["theme"])
        self.entry_input.delete(0, "end")
        self.entry_output.delete(0, "end")
        self._output_path = None
        self._download_path = None

        self.combo_method.set(self.config["method"])
        self.combo_encoder.set(self.config["encoder"])
        self._on_encoder_change(self.config["encoder"])
        self.combo_video_quality.set(self.config["video_quality"])
        self.combo_audio_quality.set(self.config.get("audio_quality", "medium"))
        self.combo_download_quality.set(self.config["download_quality"])
        self.combo_output_format.set(self.config.get("output_format", "video"))
        self._set_checkbox(self.chk_force, self.config["force"])
        self._set_checkbox(self.chk_delete, self.config["delete_after"])
        self._set_checkbox(self.chk_per_video_dir, self.config["per_video_dir"])
        self._set_checkbox(self.chk_completion_sound, self.config.get("completion_sound", False))
        self._set_checkbox(self.chk_x264_low_memory, self.config.get("x264_low_memory", False))
        self._set_checkbox(self.chk_gapless_concat, self.config.get("gapless_concat", False))
        self._set_checkbox(
            self.chk_low_process_priority, self.config.get("low_process_priority", False)
        )
        self.combo_preset.set(self.config.get("preset", "balanced"))
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
                    ev.insert(0, format_slider_entry_value(val))
        self.lbl_output.configure(text="Output: —")
        self.lbl_file.configure(text="File: —")
        self.lbl_size.configure(text="Size: —")
        self.lbl_duration.configure(text="Duration: —")
        self.lbl_silence.configure(text="Silence: —")
        self.lbl_encoder.configure(text="Encoder: —")
        self._save_settings()
        self._log("Settings restored to defaults")

    @staticmethod
    def _set_checkbox(checkbox: Any, value: bool) -> None:
        """Set a CTkCheckBox to True/False (deselect/select)."""
        if value:
            checkbox.select()
        else:
            checkbox.deselect()

    @staticmethod
    def _fit_to_screen(sw: int, sh: int) -> tuple[int, int]:
        """Delegates to gui_platform.fit_to_screen (pure, testable)."""
        return fit_to_screen(sw, sh)

    def _save_user_defaults(self) -> None:
        """Snapshot the current tunable GUI values to user_defaults.json."""
        try:
            self._sync_slider_entries()
        except Exception:
            pass
        widgets = {
            "threshold": float(self.config["threshold"]),
            "min_silence": float(self.config["min_silence"]),
            "margin": float(self.config["margin"]),
            "method": self.combo_method.get(),
            "encoder": self.combo_encoder.get(),
            "video_quality": self.combo_video_quality.get(),
            "audio_quality": self.combo_audio_quality.get(),
            "download_quality": self.combo_download_quality.get(),
            "output_format": self.combo_output_format.get(),
            "force": bool(self.chk_force.get()),
            "delete_after": bool(self.chk_delete.get()),
            "per_video_dir": bool(self.chk_per_video_dir.get()),
            "completion_sound": bool(self.chk_completion_sound.get()),
            "x264_low_memory": bool(self.chk_x264_low_memory.get()),
            "gapless_concat": bool(self.chk_gapless_concat.get()),
            "low_process_priority": bool(self.chk_low_process_priority.get()),
            "preset": self.combo_preset.get(),
            "theme": self.combo_theme.get(),
        }
        snapshot = build_user_defaults_snapshot(widgets)
        try:
            save_user_defaults(snapshot)
        except Exception as e:
            self._log(f"[WARN] Could not save user defaults: {e}")
            return
        self._log(f"Saved current settings as user defaults ({user_defaults_path()})")

    def _copy_cli_command(self) -> None:
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
        x264_low_memory = bool(self.chk_x264_low_memory.get())
        gapless_concat = bool(self.chk_gapless_concat.get())
        low_process_priority = bool(self.chk_low_process_priority.get())
        preset = self.combo_preset.get()
        memory_limit_mb = self.config.get("memory_limit_mb", "auto")
        memory_reserve_mb = self.config.get("memory_reserve_mb", 2048)

        out_path = Path(out_raw).expanduser()
        config_path = write_cli_config_yaml(
            out_path,
            float(self.config["threshold"]),
            float(self.config["min_silence"]),
            float(self.config["margin"]),
        )
        if config_path is None:
            self._log("[WARN] Could not write CLI config (out_dir not writable)")

        cmd = build_cli_command(
            inp,
            out_path,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            download_quality=download_quality,
            output_format=self.combo_output_format.get(),
            force=force,
            delete_after=delete_after,
            x264_low_memory=x264_low_memory,
            gapless_concat=gapless_concat,
            low_process_priority=low_process_priority,
            preset=preset,
            memory_limit_mb=memory_limit_mb,
            memory_reserve_mb=memory_reserve_mb,
            segment_encode_timeout=self.config.get("segment_encode_timeout", 600),
            final_concat_timeout=self.config.get("final_concat_timeout", 86400),
            silence_timeout=self.config.get("silence_timeout", 36000),
            stall_kill_timeout=self.config.get("stall_kill_timeout", 300),
            waveform_timeout=self.config.get("waveform_timeout", 300),
            batch_chunk_size=self.config.get("batch_chunk_size", 40),
            min_part_bytes=self.config.get("min_part_bytes", 1024),
            config_path=config_path,
        )
        self.clipboard_clear()
        self.clipboard_append(cmd)
        if config_path is not None:
            self._log(f"CLI command copied. Config written to: {config_path}")
        else:
            self._log(f"CLI command copied (config NOT written — see warning): {cmd}")
        self._log(f"  {cmd}")

    def _settings_path(self) -> Path:
        return settings_path()

    def _on_close(self) -> None:
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
                _logger.info(f"Cleaned up incomplete output: {self._output_path}")
            except OSError:
                pass
        # Clean up incomplete download file
        if self._download_path is not None and self._download_path.exists():
            try:
                self._download_path.unlink()
                _logger.info(f"Cleaned up incomplete download: {self._download_path}")
            except OSError:
                pass
        try:
            self._save_settings()
        except Exception as e:
            _logger.warning("Failed to save settings on close: %s", e)
        self.destroy()
