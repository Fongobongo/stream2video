"""LifecycleMixin — settings I/O, restore defaults, on-close (Этап 10 mixin).

Extracted from ``Stream2VideoGUI``: ``_save_settings`` (snapshot widgets
→ settings.json), ``_load_settings`` (read settings.json → self.config),
``_restore_defaults``, ``_save_user_defaults``, ``_copy_cli_command``,
``_on_close`` (WM_DELETE_WINDOW handler — cancel running pipeline,
delete incomplete output, save settings, destroy).
"""

from __future__ import annotations

import logging
from pathlib import Path
from tkinter import messagebox
from typing import Any, cast

import customtkinter as ctk

from stream2video.config import (
    effective_defaults,
    save_user_defaults,
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
from stream2video.utils import cancel_process, list_active_owners

_logger = logging.getLogger("stream2video.gui")


class _ProxyInputDialog(ctk.CTkInputDialog):
    """CTkInputDialog with the previous proxy address already inserted.

    The value is inserted after the stock dialog builds its widgets
    (so ``_entry`` exists) and the whole text is selected for quick
    overwrite while still allowing in-place editing.
    """

    def __init__(self, initial: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._initial = initial

    def _create_widgets(self) -> None:
        super()._create_widgets()
        if self._initial:
            self._entry.insert(0, self._initial)
            self._entry.select_range(0, "end")


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
            "use_crf": bool(self.chk_use_crf.get()),
            "gapless_concat": bool(self.chk_gapless_concat.get()),
            "low_process_priority": bool(self.chk_low_process_priority.get()),
            "preset": self.combo_preset.get(),
            "theme": self.combo_theme.get(),
            "proxy": str(self.config.get("proxy", "")),
            "proxy_active": bool(self.config.get("proxy_active", False)),
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
        self._active_controller: object | None = None

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
        self._set_checkbox(self.chk_use_crf, self.config.get("use_crf", False))
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
        if hasattr(self, "chk_proxy"):
            if self.config.get("proxy_active", False):
                self.chk_proxy.select()
            else:
                self.chk_proxy.deselect()
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

    def _on_proxy_toggle(self) -> None:
        """Checkbox handler: persist the proxy on/off state."""
        self.config["proxy_active"] = bool(self.chk_proxy.get())
        self._save_settings()

    def _set_proxy(self) -> None:
        """Open a dialog to set the proxy server used for downloads.

        The entry is prefilled with the previously entered address so
        it can be edited. Empty = no proxy address; the value is always
        kept in ``self.config["proxy"]`` (even while the proxy is
        disabled) so it isn't lost. Setting a non-empty address
        auto-enables the proxy checkbox; the address is passed to
        yt-dlp as ``--proxy`` only while ``proxy_active`` is on.
        """
        dialog = _ProxyInputDialog(
            initial=str(self.config.get("proxy", "")),
            title="Download proxy",
            text=(
                "Proxy server for downloads (empty = no proxy).\n"
                "Examples: http://127.0.0.1:8080 or "
                "socks5://user:pass@host:1080."
            ),
        )
        value = dialog.get_input()
        if value is None:
            return  # cancelled
        value = value.strip()
        self.config["proxy"] = value
        if value and not self.config.get("proxy_active", False):
            self.config["proxy_active"] = True
            if hasattr(self, "chk_proxy"):
                self.chk_proxy.select()
        self._save_settings()
        self._log(f"Download proxy set to: {value or 'off (direct connection)'}")

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
            "use_crf": bool(self.chk_use_crf.get()),
            "gapless_concat": bool(self.chk_gapless_concat.get()),
            "low_process_priority": bool(self.chk_low_process_priority.get()),
            "preset": self.combo_preset.get(),
            "theme": self.combo_theme.get(),
            "proxy": str(self.config.get("proxy", "")),
            "proxy_active": bool(self.config.get("proxy_active", False)),
        }
        snapshot = build_user_defaults_snapshot(widgets, current=self.config)
        try:
            save_user_defaults(snapshot)
        except Exception as e:
            self._log(f"[WARN] Could not save user defaults: {e}")
            return
        self._log(f"Saved current settings as user defaults ({user_defaults_path()})")

    def _copy_cli_command(self) -> None:
        self._sync_slider_entries()
        inp = self.entry_input.get().strip()
        out_raw = self.entry_output.get().strip() or "./processed_videos"
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        video_quality = self.combo_video_quality.get()
        audio_quality = self.combo_audio_quality.get()
        download_quality = self.combo_download_quality.get()
        force = bool(self.chk_force.get())
        delete_after = bool(self.chk_delete.get())
        x264_low_memory = bool(self.chk_x264_low_memory.get())
        use_crf = bool(self.chk_use_crf.get())
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
            use_crf=use_crf,
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
            proxy=self.config.get("proxy", "") if self.config.get("proxy_active", False) else "",
        )
        self.clipboard_clear()
        self.clipboard_append(cmd)
        if config_path is not None:
            self._log(f"CLI command copied. Config written to: {config_path}")
        else:
            self._log(f"CLI command copied (config NOT written — see warning): {cmd}")
        self._log(f"  {cmd}")

    def _on_close(self) -> None:
        if self.running:
            answer = messagebox.askyesno(
                "Quit?",
                "Pipeline is running. Stop and quit?",
                # parent=self: an unparented dialog can fall behind the
                # main window — the user then clicks X again, re-enters
                # _on_close and gets a second dialog, and it looks like
                # the app "won't close". Mirrors gui.py's Uncaught-Exception
                # dialog. The host (``Stream2VideoGUI``) is always a CTk at
                # runtime; the cast only helps mypy see that (the mixin is
                # deliberately not subclassing CTk so its runtime MRO stays
                # flat).
                parent=cast(ctk.CTk, self),
            )
            if not answer:
                return
        self._cancel_event.set()
        # Kill ALL registered processes, not just the default owner —
        # a running waveform preview (owner="preview") or a mid-flight
        # download otherwise survives for its full timeout (up to 300s)
        # holding the user's media file open.
        for owner in list_active_owners():
            try:
                cancel_process(owner, timeout=3)
            except Exception:
                _logger.debug("cancel_process(%r) on close failed", owner, exc_info=True)
        # Clean up incomplete artifacts — the REAL paths live in the
        # active PipelineController (``_download_path`` / ``_output_path``
        # there are stamped by the download/concat phases). The GUI's own
        # ``_output_path`` / ``_download_path`` fields are never populated
        # (dead on-close cleanup), so ask the controller directly (B9
        # audit). The controller is registered by the worker at Start and
        # cleared in its finally; on-close racing that clear is harmless
        # (both paths are idempotent / exception-guarded).
        active = getattr(self, "_active_controller", None)
        if active is not None:
            try:
                cleanup = getattr(active, "cleanup_incomplete_on_close", None)
                if callable(cleanup):
                    cleanup()
            except Exception:
                _logger.debug("controller cleanup on close failed", exc_info=True)
        # fix-plan #20: flush any uncommitted slider entry text into
        # config BEFORE _save_settings reads it. A user typing into the
        # numeric entry and closing the window (without FocusOut, which
        # normally triggers the commit) silently lost the typed value
        # and the config written to disk held the previous one.
        self._sync_slider_entries()
        try:
            self._save_settings()
        except Exception as e:
            _logger.warning("Failed to save settings on close: %s", e)
        # Detach the log-queue handler so a future GUI window / test in
        # this same process doesn't keep piping records into a dead queue.
        if self._log_poller is not None:
            self._log_poller.teardown_logging()
        self.destroy()
