"""LifecycleMixin — settings I/O, restore defaults, on-close.

Extracted from ``Stream2VideoGUI``: ``_save_settings`` (snapshot widgets
→ settings.json), ``_load_settings`` (read settings.json → self.settings),
``_restore_defaults``, ``_save_user_defaults``, ``_copy_cli_command``,
``_on_close`` (WM_DELETE_WINDOW handler — cancel running pipeline,
delete incomplete output, save settings, destroy).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from tkinter import messagebox
from typing import Any, cast

import customtkinter as ctk

from stream2video.config import (
    effective_defaults,
    save_user_defaults,
    user_defaults_path,
)
from stream2video.download import PROXY_SCHEMES, validate_proxy_url
from stream2video.gui_helpers import (
    build_cli_command,
    mask_proxy,
    proxy_has_credentials,
    redact_proxy_in_cli_command,
    strip_proxy_credentials,
)
from stream2video.gui_platform import fit_to_screen
from stream2video.gui_settings import load_settings as _load_settings_from_disk
from stream2video.gui_settings import save_settings as _save_settings_to_disk
from stream2video.settings_io import (
    build_save_settings_snapshot,
    build_settings_payload,
    build_user_defaults_snapshot,
)
from stream2video.slider_widgets import SLIDER_KEYS, format_slider_entry_value
from stream2video.utils import cancel_process, list_active_owners

_logger = logging.getLogger("stream2video.gui")


# Scheme selector order for the proxy dialog: the schemes people actually
# buy first, the exotic ones (socks4/4a) after.
_PROXY_SCHEME_UI_ORDER: list[str] = [
    s for s in ("http", "https", "socks5", "socks5h", "socks4", "socks4a") if s in PROXY_SCHEMES
]


def _split_proxy_url(text: str) -> tuple[str | None, str]:
    """Split a pasted proxy address into ``(scheme, rest)``.

    ``scheme`` is None when the text does NOT start with a known proxy
    scheme (then the text is kept verbatim — the selector's scheme will
    be applied on top by :func:`_join_proxy_url`). A known scheme is
    stripped and lowercased so a pasted ``SOCKS5://…`` selects the
    ``socks5`` entry. Pure function, no I/O.
    """
    value = text.strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", value)
    if not m:
        return None, value
    scheme = m.group(1).lower()
    if scheme not in PROXY_SCHEMES:
        return None, value
    return scheme, value[m.end() :]


def _join_proxy_url(scheme: str, rest: str) -> str:
    """Assemble the proxy URL from the selector's scheme and the entry's
    text. An empty rest means "no proxy" and assembles to ``""``."""
    value = rest.strip()
    if not value:
        return ""
    return f"{scheme}://{value}"


class _ProxyDialog(ctk.CTkToplevel):
    """Proxy-address dialog: a scheme dropdown + one address field.

    The user no longer types ``http://`` by hand: the dropdown carries
    the scheme and the entry takes ``[user:pass@]host[:port]``. Pasting
    a full address (``socks5://user:pass@host:1080``) auto-selects the
    matching scheme and strips the prefix from the field. OK validates
    the assembled URL with the shared rule (download.validate_proxy_url)
    and shows the error INLINE — the dialog stays open until the value
    is either valid or cancelled. ``get_input()`` mirrors the historical
    CTkInputDialog contract: modal wait, returns the full URL (with the
    scheme), ``""`` when the field was emptied (no proxy), None on cancel.
    """

    def __init__(self, master: Any = None, initial: str = "") -> None:
        super().__init__(master)
        self._result: str | None = None
        self.title("Download proxy")
        # No fixed geometry: the inline error label can grow to several
        # lines (the scheme-list message is ~250 chars), and a fixed
        # height would clip the OK/Cancel row off the bottom of the
        # window. The window auto-sizes to the grid; minsize keeps the
        # no-error state compact and stable.
        self.minsize(480, 200)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel_event)

        scheme, rest = _split_proxy_url(initial)
        self._scheme = ctk.StringVar(value=scheme or _PROXY_SCHEME_UI_ORDER[0])

        ctk.CTkLabel(self, text="Proxy server for downloads (empty = no proxy).").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)
        )
        self._scheme_menu = ctk.CTkOptionMenu(
            self, width=110, values=_PROXY_SCHEME_UI_ORDER, variable=self._scheme
        )
        self._scheme_menu.grid(row=1, column=0, sticky="w", padx=12, pady=6)
        self._entry = ctk.CTkEntry(self, width=310, placeholder_text="user:pass@host:port")
        self._entry.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=6)
        self._entry.insert(0, rest)
        self._entry.select_range(0, "end")
        self._entry.focus_set()

        ctk.CTkLabel(
            self,
            text="Paste a full address (http://… or socks5://…) — the type is detected.",
            text_color=("gray40", "gray60"),
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 4))

        self._error_label = ctk.CTkLabel(
            self, text="", text_color="#e5484d", wraplength=430, justify="left"
        )
        self._error_label.grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 2))

        self._ok_button = ctk.CTkButton(self, text="OK", width=90, command=self._ok_event)
        self._ok_button.grid(row=4, column=0, sticky="e", padx=(12, 4), pady=(4, 12))
        self._cancel_button = ctk.CTkButton(
            self, text="Cancel", width=90, command=self._cancel_event, fg_color="transparent"
        )
        self._cancel_button.grid(row=4, column=1, sticky="e", padx=(0, 12), pady=(4, 12))

        # Auto-detect the scheme of a pasted full address. Programmatic
        # delete/insert below does NOT re-fire these bindings.
        self._entry.bind("<KeyRelease>", lambda _e: self._on_entry_changed())
        self._entry.bind("<<Paste>>", lambda _e: self.after_idle(self._on_entry_changed))
        self.bind("<Return>", lambda _e: self._ok_event())

        try:
            self.grab_set()  # modal (may fail on exotic WMs — non-fatal)
        except Exception:
            pass

    # -- events ------------------------------------------------------------

    def _on_entry_changed(self) -> None:
        scheme, rest = _split_proxy_url(self._entry.get())
        if scheme is not None:
            self._scheme.set(scheme)
            self._entry.delete(0, "end")
            self._entry.insert(0, rest)
        self._error_label.configure(text="")

    def _ok_event(self) -> None:
        url = _join_proxy_url(self._scheme.get(), self._entry.get())
        error = validate_proxy_url(url)
        if error is not None:
            self._error_label.configure(text=error)
            self._entry.focus_set()
            return
        self._result = url
        self.destroy()

    def _cancel_event(self) -> None:
        self._result = None
        self.destroy()

    def get_input(self) -> str | None:
        """Block until the dialog closes; return the URL / "" / None."""
        self.wait_window(self)
        return self._result


class LifecycleMixin:
    """Settings persistence + window lifecycle (close / restore / save)."""

    def _read_widget_values(self) -> dict[str, Any]:
        """Read every tunable widget in the Tk main thread (Tk reads are
        unsafe from worker threads).

        Shared by ``_save_settings`` / ``_save_user_defaults`` /
        ``_copy_cli_command`` / ``_start_pipeline`` so the widget-reading
        blocks can't drift apart again — the audit found three copies
        that had already diverged (``_copy_cli_command`` pulled
        ``software_fallback`` from ``self.settings`` while
        ``_save_settings`` never saved it at all).
        """
        values: dict[str, Any] = {
            "threshold": float(self.settings["threshold"]),
            "min_silence": float(self.settings["min_silence"]),
            "margin": float(self.settings["margin"]),
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
            "proxy": str(self.settings.get("proxy", "")),
            "proxy_active": bool(self.settings.get("proxy_active", False)),
        }
        values.update(self._read_advanced_widget_values())
        return values

    def _save_settings(self) -> None:
        # Read widgets in the main thread (Tk reads are unsafe from
        # worker threads); forward the snapshot through the pure
        # :func:`stream2video.settings_io.build_save_settings_snapshot`
        # so the field list / key order is unit-tested in isolation.
        widgets = {
            "input_path": self.entry_input.get().strip(),
            "output_dir": self.entry_output.get().strip(),
            "window_geometry": self.geometry(),
        }
        widgets.update(self._read_widget_values())
        snapshot = build_save_settings_snapshot(widgets)
        self.settings.update(snapshot)
        # Persist only the DELTA vs the effective defaults (plus the
        # session keys): a tunable the user never touched keeps its
        # user_defaults.json value even after this write, so
        # settings.json can't shadow user_defaults.json. Previously the
        # whole ``self.settings`` dict was dumped, and any key ever
        # written to settings.json permanently overrode the user
        # defaults file.
        payload = build_settings_payload({**self.settings, **snapshot})
        try:
            _save_settings_to_disk(payload)
        except Exception as e:
            self._log(f"[WARN] Could not save settings: {e}")

    def _load_settings(self) -> None:
        loaded = _load_settings_from_disk()
        for key, value in loaded.items():
            self.settings[key] = value

    def _restore_defaults(self) -> None:
        # Recent Projects is SESSION STATE pointing at real directories on
        # disk, not a tunable: effective_defaults() carries an empty list,
        # and _save_settings() below used to persist that — silently
        # wiping the on-disk list while the panel kept showing the old
        # rows until restart. Carry the currently-displayed entries over.
        previous_recent = (
            list(self.settings.get("recent_projects", []))
            if isinstance(self.settings, dict)
            else []
        )
        self.settings = effective_defaults()
        if previous_recent:
            self.settings["recent_projects"] = previous_recent
        # Route through the shared theme handler instead of re-implementing
        # it: the inline copy set the mode + combo but skipped the log
        # poller's tag re-skin, so [WARN]/[ERROR] colours stayed from the
        # old theme until the next manual switch.
        self._on_theme_change(self.settings["theme"])
        self.combo_theme.set(self.settings["theme"])
        self.entry_input.delete(0, "end")
        self.entry_output.delete(0, "end")
        # Don't null the controller while a run is active: the worker
        # thread still targets it (progress callbacks, on-close cleanup
        # in ``_on_close`` reads it). Only a *finished* run's dangling
        # reference is reset here.
        if not getattr(self, "running", False):
            self._active_controller: object | None = None

        self.combo_method.set(self.settings["method"])
        self.combo_encoder.set(self.settings["encoder"])
        self._on_encoder_change(self.settings["encoder"])
        self.combo_video_quality.set(self.settings["video_quality"])
        self.combo_audio_quality.set(self.settings.get("audio_quality", "source"))
        self.combo_download_quality.set(self.settings["download_quality"])
        self.combo_output_format.set(self.settings.get("output_format", "video"))
        self._set_checkbox(self.chk_force, self.settings["force"])
        self._set_checkbox(self.chk_delete, self.settings["delete_after"])
        self._set_checkbox(self.chk_per_video_dir, self.settings["per_video_dir"])
        self._set_checkbox(self.chk_completion_sound, self.settings.get("completion_sound", True))
        self._set_checkbox(self.chk_x264_low_memory, self.settings.get("x264_low_memory", False))
        self._set_checkbox(self.chk_use_crf, self.settings.get("use_crf", False))
        self._set_checkbox(self.chk_gapless_concat, self.settings.get("gapless_concat", True))
        self._set_checkbox(
            self.chk_low_process_priority, self.settings.get("low_process_priority", False)
        )
        self.combo_preset.set(self.settings.get("preset", "balanced"))
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w, win_h = self._fit_to_screen(sw, sh)
        self.geometry(f"{win_w}x{win_h}")
        for key in SLIDER_KEYS:
            slider = getattr(self, f"_slider_{key}", None)
            if slider:
                val = self.settings[key]
                slider.set(val)
                ev = getattr(slider, "_entry_val", None)
                if ev:
                    ev.delete(0, "end")
                    ev.insert(0, format_slider_entry_value(val))
        self._set_advanced_widget_values(self.settings)
        self.lbl_output.configure(text="Output: —")
        self.lbl_file.configure(text="File: —")
        self.lbl_size.configure(text="Size: —")
        self.lbl_duration.configure(text="Duration: —")
        self.lbl_silence.configure(text="Silence: —")
        self.lbl_encoder.configure(text="Encoder: —")
        if hasattr(self, "chk_proxy"):
            if self.settings.get("proxy_active", False):
                self.chk_proxy.select()
            else:
                self.chk_proxy.deselect()
        # Re-render the panel so it matches what was preserved/persisted.
        if hasattr(self, "_render_recent_projects"):
            self._render_recent_projects()
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
        self.settings["proxy_active"] = bool(self.chk_proxy.get())
        self._save_settings()

    def _set_proxy(self) -> None:
        """Open a dialog to set the proxy server used for downloads.

        The entry is prefilled with the previously entered address so
        it can be edited. Empty = no proxy address; the value is always
        kept in ``self.settings["proxy"]`` (even while the proxy is
        disabled) so it isn't lost. Setting a non-empty address
        auto-enables the proxy checkbox; the address is passed to
        yt-dlp as ``--proxy`` only while ``proxy_active`` is on.
        """
        dialog = _ProxyDialog(
            master=self.winfo_toplevel(),
            initial=str(self.settings.get("proxy", "")),
        )
        value = dialog.get_input()
        if value is None:
            return  # cancelled
        value = value.strip()
        # Format gate (the SAME rule load_config and validate_pipeline_config
        # enforce; the dialog already validates inline — this is the single
        # choke point for hosts/tests that call _set_proxy with a patched
        # dialog): a typo'd address is useless the moment the proxy is
        # switched on, so reject it at entry instead of storing a time bomb.
        proxy_error = validate_proxy_url(value)
        if proxy_error is not None:
            # parent via winfo_toplevel (the mixin's own typed pattern —
            # see gui_recent_projects): an unparented dialog can fall
            # behind the main window while blocking input.
            messagebox.showerror("Invalid proxy address", proxy_error, parent=self.winfo_toplevel())
            return
        self.settings["proxy"] = value
        if value and not self.settings.get("proxy_active", False):
            self.settings["proxy_active"] = True
            if hasattr(self, "chk_proxy"):
                self.chk_proxy.select()
        self._save_settings()
        self._log(f"Download proxy set to: {mask_proxy(value) or 'off (direct connection)'}")

    def _save_user_defaults(self) -> None:
        """Snapshot the current tunable GUI values to user_defaults.json."""
        # Validation gate (audit round 22 P8): the same Advanced-widget
        # check that blocks Start / Copy CLI must block "Save current as
        # defaults" too — otherwise invalid visible text (e.g.
        # ``download_timeout=abc``) parses to the last ``current`` value
        # and the dialog reports success while the OLD number is written
        # to disk, silently diverging from what the user sees.
        adv_errors = self._advanced_widget_errors()
        if adv_errors:
            for err in adv_errors.values():
                self._log(f"[ERROR] Invalid setting: {err}")
            messagebox.showerror(
                "Invalid settings",
                "Cannot save user defaults — some Advanced settings "
                "are invalid:\n\n" + "\n".join(adv_errors.values()) + "\n\n"
                "Fix them and try again. Nothing was written.",
                parent=cast(ctk.CTk, self),
            )
            return
        try:
            self._sync_slider_entries()
        except Exception:
            pass
        widgets = self._read_widget_values()
        snapshot = build_user_defaults_snapshot(widgets, current=self.settings)
        try:
            save_user_defaults(snapshot)
        except Exception as e:
            self._log(f"[WARN] Could not save user defaults: {e}")
            return
        self._log(f"Saved current settings as user defaults ({user_defaults_path()})")

    def _copy_cli_command(self) -> None:
        self._sync_slider_entries()
        # Validation gate (audit P2): a copied command must not silently
        # carry a fallback value while the widget shows invalid text —
        # refuse the copy and tell the user which fields to fix. The
        # input is required here (audit round 27 P8): without it the
        # copied command has no positional argument and the CLI rejects
        # it as a missing argument.
        adv_errors = self._advanced_widget_errors(require_input=True)
        if adv_errors:
            for err in adv_errors.values():
                self._log(f"[ERROR] Invalid setting: {err}")
            messagebox.showerror(
                "Invalid settings",
                "Cannot copy the CLI command — some Advanced settings "
                "are invalid:\n\n" + "\n".join(adv_errors.values()) + "\n\nFix them and try again.",
                parent=cast(ctk.CTk, self),
            )
            return
        # The SAME widget values the Start button's run_config reads
        # (shared ``_read_widget_values``), so the copied command
        # reproduces the GUI run exactly. Previously the 18 advanced
        # values came from ``self.settings`` (which ``_save_settings``
        # never even wrote for several of them) and the copied command
        # could silently disagree with the run.
        values = self._read_widget_values()
        inp = self.entry_input.get().strip()
        out_raw = self.entry_output.get().strip() or "./processed_videos"

        out_path = Path(out_raw).expanduser()
        proxy_value = values["proxy"] if values["proxy_active"] else ""
        # The gate travels as an explicit flag: when the checkbox OFF
        # diverges from the effective default (e.g. a stored
        # ``proxy_active: true`` in user_defaults.json), the builder
        # emits --no-proxy-active so the paste can't re-enable the
        # stored address (audit P1). When ON, the address itself travels
        # via --proxy below.
        proxy_active_value = bool(values["proxy_active"])
        # Audit #3: the copied command lands in the clipboard, the shell
        # history and the process list — a proxy password must NOT go
        # there silently. Copy without credentials by default (explicit
        # user confirmation re-includes them).
        proxy_copied = proxy_value
        if proxy_has_credentials(proxy_value):
            include_secret = messagebox.askyesno(
                "Proxy credentials",
                "Your proxy address contains a password.\n\n"
                "Include it in the copied CLI command? It will remain in "
                "the clipboard, the shell history and the process list.\n\n"
                "Choose No to copy the command without the password "
                "(the proxy URL is kept, the credential part is removed).",
                icon="warning",
                parent=cast(ctk.CTk, self),
            )
            if not include_secret:
                proxy_copied = strip_proxy_credentials(proxy_value)
                self._log(
                    "[WARN] Proxy password NOT copied to the clipboard — "
                    "the command keeps the proxy URL without credentials"
                )
        cmd = build_cli_command(
            inp,
            out_path,
            method=values["method"],
            encoder=values["encoder"],
            video_quality=values["video_quality"],
            audio_quality=values["audio_quality"],
            download_quality=values["download_quality"],
            software_fallback=values["software_fallback"],
            x264_preset=values["x264_preset"],
            encoder_threads=values["encoder_threads"],
            output_fps=values["output_fps"],
            output_format=values["output_format"],
            # Slider values are passed as explicit CLI flags — no side-car
            # YAML file needed (a failed YAML write used to leave the
            # copied command silently running with different values).
            threshold=values["threshold"],
            min_silence=values["min_silence"],
            margin=values["margin"],
            force=values["force"],
            delete_after=values["delete_after"],
            x264_low_memory=values["x264_low_memory"],
            use_crf=values["use_crf"],
            gapless_concat=values["gapless_concat"],
            low_process_priority=values["low_process_priority"],
            completion_sound=values["completion_sound"],
            preset=values["preset"],
            memory_limit_mb=values["memory_limit_mb"],
            memory_reserve_mb=values["memory_reserve_mb"],
            rlimit_as_mb=values["rlimit_as_mb"],
            download_timeout=values["download_timeout"],
            connect_timeout=values["connect_timeout"],
            no_progress_timeout=values["no_progress_timeout"],
            segment_encode_timeout=values["segment_encode_timeout"],
            final_concat_timeout=values["final_concat_timeout"],
            silence_timeout=values["silence_timeout"],
            stall_kill_timeout=values["stall_kill_timeout"],
            stall_warning_timeout=values["stall_warning_timeout"],
            waveform_timeout=values["waveform_timeout"],
            batch_chunk_size=values["batch_chunk_size"],
            min_part_bytes=values["min_part_bytes"],
            proxy=proxy_copied,
            proxy_active=proxy_active_value,
            per_video_dir=values["per_video_dir"],
        )
        cmd_log = redact_proxy_in_cli_command(cmd, proxy_copied)
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self._log(f"CLI command copied: {cmd_log}")

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
        # there are stamped by the download/concat phases). Ask the
        # controller directly (B9 audit); the controller is registered by
        # the worker at Start and cleared in its finally; on-close racing
        # that clear is harmless (both paths are idempotent /
        # exception-guarded).
        active = getattr(self, "_active_controller", None)
        if active is not None:
            try:
                cleanup = getattr(active, "cleanup_incomplete_on_close", None)
                if callable(cleanup):
                    cleanup()
            except Exception:
                _logger.debug("controller cleanup on close failed", exc_info=True)
        # Flush any uncommitted slider entry text into
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
