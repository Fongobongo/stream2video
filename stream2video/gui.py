"""stream2video GUI — cross-platform desktop application.

The ``Stream2VideoGUI`` class is composed from nine focused mixins
(see ``gui_*.py`` modules) via multiple inheritance. This file owns:

  * The composite class declaration (MRO) and the ``__init__``
    orchestrator that calls each mixin's ``_init_*`` in the order the
    cross-mixin contract requires.
  * The cross-cutting helpers every mixin calls (``_tk_after`` /
    ``_log`` / ``_wave_window_alive``) and the cross-thread dispatch
    infrastructure (``TkDispatcher`` / ``LogQueuePoller`` /
    ``log_queue`` / ``_live_segments_store``).
  * The two adapter classes (``_EncoderTesterAdapter`` /
    ``_PipelineGuiCallbacksAdapter``) that bridge worker threads to
    the GUI's main-thread-only surface.
  * ``_start_pipeline`` / ``_pipeline_worker`` — the orchestrator glue
    that reads widgets, spawns the worker, and stamps per-run state
    owned by :class:`ProgressUiMixin`.
  * Module-level ``main()`` entry point.
"""

import logging
import queue
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from stream2video.config import apply_preset, effective_defaults
from stream2video.gui_dialogs import DialogsMixin
from stream2video.gui_encoder_panel import EncoderPanelMixin
from stream2video.gui_file_info import FileInfoMixin
from stream2video.gui_lifecycle import LifecycleMixin
from stream2video.gui_main_window_build import MainWindowBuildMixin
from stream2video.gui_progress_ui import ProgressUiMixin
from stream2video.gui_recent_projects import RecentProjectsMixin
from stream2video.gui_sliders import SlidersMixin
from stream2video.gui_waveform import WaveformMixin
from stream2video.pipeline_worker import (
    PipelineWorker,
    PipelineWorkerParams,
)
from stream2video.silence import SilenceSegment
from stream2video.tk_dispatch import LogQueuePoller, TkDispatcher
from stream2video.waveform_popup import LiveSegmentsStore

logger = logging.getLogger("stream2video.gui")


class Stream2VideoGUI(
    MainWindowBuildMixin,
    WaveformMixin,
    RecentProjectsMixin,
    EncoderPanelMixin,
    DialogsMixin,
    FileInfoMixin,
    ProgressUiMixin,
    SlidersMixin,
    LifecycleMixin,
    ctk.CTk,
):
    """Composite GUI class built from focused mixins.

    The MRO is non-load-bearing (no mixin overrides another mixin's
    method, none overrides a ``ctk.CTk`` method). The order keeps the
    heavy builder (``MainWindowBuildMixin``) first and the configurator
    (``LifecycleMixin``) last so the lifecycle reads as a natural
    init → build → run → close flow.
    """

    def __init__(self) -> None:
        super().__init__()

        self.title("stream2video")

        # Per-mixin state init — call each mixin's ``_init_*`` in the
        # order the cross-mixin contract requires (see the mixin docs).
        # ProgressUiMixin owns running/cancel/event state — needed by
        # WaveformMixin's poller (reads self.running) and by
        # ``_start_pipeline`` (sets self.running).
        self._init_progress_ui()
        # EncoderPanelMixin owns the lazy ``EncoderTester`` slot.
        self._init_encoder_panel()
        # WaveformMixin owns ~30 ``_wave_*``/``_waveform_*`` attributes
        # the popup reads. Must run before ``_build_ui`` (the input
        # StringVar trace references ``_update_waveform_button_state``
        # which reads ``self._wave_window`` via ``_wave_window_alive``).
        self._init_waveform_state()

        # Host-owned cross-thread state — shared with the adapters and
        # the pipeline worker. ``_live_segments_store`` is read by
        # ``WaveformMixin._take_live_snapshot`` and mutated by
        # ``_PipelineGuiCallbacksAdapter.set_live_segments`` /
        # ``pop_live_segments`` (which the worker calls via the adapter).
        self._live_segments_store = LiveSegmentsStore()

        # LifecycleMixin owns the config dict — read by every mixin and
        # mutated by ``_load_settings`` / ``_restore_defaults`` /
        # ``_save_settings``.
        self.config = effective_defaults()

        # Cross-thread dispatch infrastructure (host-owned).
        # ``TkDispatcher`` swallows ``TclError`` if the root was
        # destroyed mid-pipeline; ``LogQueuePoller`` drains the queue
        # into the textbox on the Tk main loop.
        self._dispatcher = TkDispatcher(self)
        self.log_queue: queue.Queue = queue.Queue()
        # ``LogQueuePoller`` needs the textbox widget (built in
        # ``_build_ui`` below); created with a placeholder None and
        # rebound after ``_build_ui`` runs.
        self._log_poller: LogQueuePoller | None = None

        # Merge disk-loaded settings on top of the defaults BEFORE the
        # widgets are built (the combos / entries read from
        # ``self.config`` during construction).
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

        # Build every widget (MainWindowBuildMixin). Must run before any
        # mixin touches the widgets it builds (ProgressUiMixin's
        # ``_set_running`` reads ``btn_start`` / ``btn_cancel``;
        # LifecycleMixin's ``_save_settings`` reads ``entry_*`` /
        # ``combo_*``; WaveformMixin's ``_update_waveform_button_state``
        # reads ``btn_waveform`` — it already has a getattr guard).
        self._build_ui()

        # Wire the log queue → textbox poller once ``txt_log`` exists.
        # ``theme`` selects the warn/error tag colours for the log text.
        self._log_poller = LogQueuePoller(
            textbox=self.txt_log,
            dispatcher=self._dispatcher,
            log_queue=self.log_queue,
            theme=self.config["theme"],
        )
        self._log_poller.setup_logging()
        self.after(100, self._log_poller.poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Cross-cutting helpers (host) ──────────────────────────────

    def _tk_after(self, ms: int, func: Callable[..., Any]) -> None:
        """Schedule ``func`` on the Tk main loop, swallowing ``TclError``
        if the window has been destroyed.

        Thin forward to :class:`stream2video.tk_dispatch.TkDispatcher`
        so the swallow-on-destroyed-root behaviour is unit-tested in
        isolation. Worker threads call this for every UI update; without
        the swallow a window closed mid-run would surface an uncaught
        ``TclError`` from the queued callback and leave a confusing
        logger traceback.
        """
        self._dispatcher.schedule(ms, func)

    def _log(self, message: str) -> None:
        # Thin forward to :class:`stream2video.tk_dispatch.LogQueuePoller`
        # so the timestamp formatting + queue push are unit-tested in
        # isolation. Safe to call from any thread (``queue.Queue.put``
        # is the lock).
        if self._log_poller is not None:
            self._log_poller.log(message)

    # ── Pipeline orchestration glue (host) ────────────────────────
    #
    # ``_start_pipeline`` reads widgets in the Tk main thread (P1.10)
    # and spawns ``_pipeline_worker`` on a background thread. The worker
    # builds a ``PipelineWorker`` (from ``pipeline_worker.py``) and
    # forwards an immutable ``PipelineWorkerParams`` snapshot; the
    # worker owns the PipelineController invocation, the callback
    # wiring, and the ``Pipeline*Error`` → status mapping.

    def _start_pipeline(self) -> None:
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
        self.lbl_progress_pct.configure(text="0%")
        self.lbl_status.configure(text="Starting...")

        # Sync slider entries → config (in case FocusOut didn't fire)
        self._sync_slider_entries()

        # Read controls (in main thread — Tk widget reads are unsafe from
        # worker threads; the worker receives the values as args, see
        # _pipeline_worker's signature and P1.10 in the fix plan).
        input_raw = self.entry_input.get().strip()
        output_dir = Path(self.entry_output.get().strip() or "./processed_videos")
        output_dir = output_dir.resolve()
        method = self.combo_method.get()
        encoder = self.combo_encoder.get()
        video_quality = self.combo_video_quality.get()
        audio_quality = self.combo_audio_quality.get()
        download_quality = self.combo_download_quality.get()
        # Sync combo selections into self.config so build_pipeline_config
        # (which reads from the config dict, not from PipelineWorkerParams)
        # picks up the current value. output_format drives the output file
        # extension and the audio-extract short-circuit in cut_and_concat,
        # so a stale self.config value would produce the wrong output.
        self.config["output_format"] = self.combo_output_format.get()
        # Apply resource preset BEFORE individual widget reads override
        # the preset's per-key values. The preset is a baseline; the
        # explicit checkboxes below (x264_low_memory, gapless_concat,
        # low_process_priority) write to self.config AFTER apply_preset,
        # so they win on a per-key basis (matches the CLI's
        # --preset-before-flag precedence).
        preset_name = self.combo_preset.get()
        if preset_name:
            self.config = apply_preset(self.config, preset_name)
        self.config["preset"] = preset_name
        self.config["gapless_concat"] = bool(self.chk_gapless_concat.get())
        self.config["low_process_priority"] = bool(self.chk_low_process_priority.get())
        self.config["x264_low_memory"] = bool(self.chk_x264_low_memory.get())
        self.config["use_crf"] = bool(self.chk_use_crf.get())
        self.config["completion_sound"] = bool(self.chk_completion_sound.get())
        force = bool(self.chk_force.get())
        per_video_dir = bool(self.chk_per_video_dir.get())
        delete_after = bool(self.chk_delete.get())

        # Pre-flight disk space warning on Start click (before any work).
        # Uses the same estimator as the in-pipeline check so the numbers
        # match. Best-effort: if the input is a local file we can stat it
        # and ffprobe its duration; for URLs or missing files skip the popup.
        try:
            from stream2video.formatters import fmt_size as _fmt
            from stream2video.utils import check_disk_space as _cds
            from stream2video.utils import estimate_disk_need as _edn
            from stream2video.utils import get_video_duration as _gvd
            from stream2video.utils import resolve_disk_probe as _rdp

            disk_input = Path(input_raw) if input_raw else None
            is_local = disk_input is not None and disk_input.exists() and disk_input.is_file()
            if is_local and disk_input is not None:
                src_size = disk_input.stat().st_size
                src_dur = _gvd(disk_input)
                # Without silence cache we don't know keep_dur — use worst
                # by passing None so estimate assumes keep_ratio=1.
                typ, worst = _edn(src_size, src_dur, None, method)
                probe = _rdp(output_dir)
                ok_typ, free = _cds(probe, typ)
                ok_worst, _ = _cds(probe, worst)
                if free is not None and (not ok_worst or not ok_typ):
                    need = worst if not ok_worst else typ
                    short = max(0, need - free)
                    msg = (
                        f"Free space on {probe} is {_fmt(free)}.\n\n"
                        f"Estimated need for this file ({_fmt(src_size)}, "
                        f"method={method}):\n"
                        f"  typical peak ~{_fmt(typ)}\n"
                        f"  worst-case peak ~{_fmt(worst)}\n\n"
                        f"{'WORST-CASE: ' if not ok_worst else ''}"
                        f"Need ~{_fmt(short)} more or the run may fail "
                        f"with 'No space left on device' during "
                        f"cutting/concatenating.\n\n"
                        f"Free up space or use a different output drive?\n\n"
                        f"Press OK to continue anyway, Cancel to abort."
                    )
                    self._log(
                        f"[WARN] Start pre-flight: free {_fmt(free)} < "
                        f"{'worst ' + _fmt(worst) if not ok_worst else 'typical ' + _fmt(typ)} "
                        f"(need ~{_fmt(short)} more)"
                    )
                    if not messagebox.askokcancel("Low disk space — may fail", msg, icon="warning"):
                        self._log("Start cancelled — low disk space")
                        self._set_running(False)
                        return
        except Exception:
            # Whole pre-flight degraded to a no-op (ffprobe missing, a
            # units bug in the estimator, messagebox quirk): the feature
            # silently skipped — log at warning so it isn't invisible.
            logger.warning("start disk preflight skipped", exc_info=True)

        self._ui_update_output(output_dir)

        self._log(
            f"Starting pipeline: input={input_raw}, output_dir={output_dir}, "
            f"method={method}, encoder={encoder}, "
            f"video_quality={video_quality}, download_quality={download_quality}, "
            f"output_format={self.config['output_format']}, "
            f"use_crf={self.config['use_crf']}, "
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
    ) -> None:
        """Worker thread: delegates to
        :class:`stream2video.pipeline_worker.PipelineWorker` which owns
        the PipelineController orchestration, the callback wiring, and
        the ``Pipeline*Error`` → status mapping. This method's job is
        now just:

          1. Wrap the GUI in a tiny adapter (``_PipelineGuiCallbacksAdapter``)
             that implements the :class:`PipelineGuiCallbacks` Protocol
             the worker expects. Every adapter method funnels back
             through ``self._tk_after`` so the worker never touches a
             widget directly (P1.10).
          2. Build the immutable ``PipelineWorkerParams`` snapshot from
             the already-snapshoted widget values.
          3. Stamp ``self._pipeline_start`` (the GUI tracks wall-clock
             elapsed for the progress bar) and clear the per-run path
             slots the worker mutates.
          4. Forward to ``PipelineWorker.run(params)``.
        """
        params = PipelineWorkerParams(
            input_raw=input_raw,
            output_dir=output_dir,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            download_quality=download_quality,
            force=force,
            per_video_dir=per_video_dir,
            delete_after=delete_after,
        )
        worker = PipelineWorker(_PipelineGuiCallbacksAdapter(self), self.config)
        self._pipeline_start = time.monotonic()
        try:
            worker.run(params)
        finally:
            # Per-run slots the worker doesn't own — cleared here so a
            # stale path doesn't leak into the next pipeline run or
            # the on-close cleanup path.
            self._output_path = None
            self._download_path = None


class _EncoderTesterAdapter:
    """Adapter that exposes the GUI's encoder-test surface to
    :class:`stream2video.encoder_test.EncoderTester`.
    """

    def __init__(self, gui: Any):
        self._gui = gui

    def log(self, message: str) -> None:
        self._gui._log(message)

    def schedule_on_main(self, ms: int, func: Callable[..., Any]) -> None:
        # ``_tk_after`` swallows ``TclError`` if the root is destroyed
        # mid-test — exactly what the legacy code did.
        self._gui._tk_after(ms, func)

    def schedule_after(self, ms: int, func: Callable[..., Any]) -> None:
        # ``self.after`` is not used cross-thread here because the
        # caller (EncoderTester) always invokes this from the worker
        # thread's ``finally`` block; ``_tk_after`` is the cross-thread
        # safe variant. Use it instead of ``self.after`` to keep the
        # pattern consistent (and avoid ``TclError`` races during
        # window close mid-test).
        self._gui._tk_after(ms, func)

    def set_test_button_state(self, *, running: bool) -> None:
        # Restore / disable the button — read from the GUI's
        # ``btn_test_encoders`` widget (created in _build_ui) so the
        # adapter doesn't need to know the button's text / state
        # constants.
        try:
            btn = self._gui.btn_test_encoders
        except AttributeError:
            return
        btn.configure(
            state=("disabled" if running else "normal"),
            text=("Testing..." if running else "Test encoder"),
        )


class _PipelineGuiCallbacksAdapter:
    """Adapter exposing the GUI's pipeline-run surface to
    :class:`stream2video.pipeline_worker.PipelineWorker`.

    The GUI can't be passed to the worker directly (it's the fat class
    at the bottom of this module; we want the worker module to depend
    only on a thin Protocol callable surface). So the GUI hands the
    worker a tiny adapter object that funnels every call back through
    the GUI's main-thread dispatcher (``_tk_after``) — the worker never
    touches a widget directly (P1.10).

    ``cancel_event`` is just the GUI's own ``self._cancel_event`` — the
    worker reads it directly so the existing ``_cancel_pipeline`` path
    keeps working without going through a dispatcher.
    """

    def __init__(self, gui: Any):
        self._gui = gui
        # Protocol-expected settable attribute; expose the GUI's event
        # so the worker (and the controller it instantiates) can poll
        # it without going through a dispatcher.
        self.cancel_event = gui._cancel_event

    def log(self, message: str) -> None:
        self._gui._log(message)

    def _fallback_consent_enc_label(self) -> str:
        """Best-effort encoder name for the consent dialog. Falls back to
        a neutral string when the widget is unavailable (test fakes,
        early-start races) so ``_tk_after`` scheduling never crashes."""
        try:
            encoder = str(self._gui.combo_encoder.get())
            quality = str(self._gui.combo_video_quality.get())
            return f"{encoder} ({quality})"
        except Exception:
            return "the selected encoder"

    def ask_fallback_consent(self) -> bool:
        """Block the worker thread while the user answers a yes/no dialog
        on the Tk main loop. Implements the ``software_fallback="ask"``
        contract for interactive hosts (mirrors ``typer.confirm`` for
        the CLI). Any dialog error (Tk already destructed, headless run)
        defaults to *refuse* so ``ask`` never silently switches encoders.
        """
        answered = threading.Event()
        consent: list[bool] = [False]

        def _ask() -> None:
            try:
                consent[0] = bool(
                    messagebox.askyesno(
                        "Encoder fallback",
                        "The selected encoder is unavailable or failed: "
                        f"{self._fallback_consent_enc_label()}.\n\n"
                        "Fall back to libx264 (CPU, slower) for this run?",
                        parent=self._gui,
                    )
                )
            except Exception:
                consent[0] = False
            finally:
                answered.set()

        try:
            self._gui._tk_after(0, _ask)
        except Exception:
            return False
        # Wait up to 60 s — plenty for a user click; a wedged Tk loop
        # (e.g. mid-shutdown) must not hang the pipeline worker forever.
        if not answered.wait(timeout=60):
            self._gui._log("[WARN] Encoder-fallback dialog timed out — refusing fallback")
            return False
        return consent[0]

    def ui_progress(self, value: float) -> None:
        self._gui._ui_progress(value)

    def ui_status(self, text: str, *, force: bool = False) -> None:
        self._gui._ui_status(text, force=force)

    def ui_info(self, text: str) -> None:
        self._gui._ui_info(text)

    def ui_overall(
        self, phase_elapsed: float, phase_remaining: float | None, more_phases: bool
    ) -> None:
        self._gui._ui_overall(phase_elapsed, phase_remaining, more_phases)

    def ui_total(self, total_elapsed: float, *, overall_est: float | None = None) -> None:
        self._gui._ui_total(total_elapsed, overall_est=overall_est)

    def ui_phase_progress(self, fraction: float) -> None:
        self._gui._set_phase_progress(fraction)

    def ui_progress_plan(self, bounds: tuple[float, float, float, float]) -> None:
        self._gui._ui_progress_plan(bounds)

    def ui_set_success_style(self) -> None:
        self._gui._ui_set_success_style()

    def ui_set_failure_style(self) -> None:
        self._gui._ui_set_failure_style()

    def ui_update_output(self, out_dir: Path) -> None:
        self._gui._ui_update_output(out_dir)

    def ui_update_file_info(self, path: Path) -> None:
        self._gui._ui_update_file_info(path)

    def add_to_recent_projects(self, project_path: Path) -> None:
        self._gui._add_to_recent_projects(project_path)

    def set_encoder_label(self, encoder: str, video_quality: str) -> None:
        self._gui._tk_after(
            0,
            lambda: self._gui.lbl_encoder.configure(text=f"Encoder: {encoder} ({video_quality})"),
        )

    def clear_overall_label(self) -> None:
        self._gui._tk_after(0, lambda: self._gui.lbl_progress_meta.configure(text=""))

    def show_complete_popup(self, text: str) -> None:
        self._gui._tk_after(0, lambda: messagebox.showinfo("Complete", text))

    def set_running(self, running: bool) -> None:
        self._gui._tk_after(0, lambda: self._gui._set_running(running))

    def set_live_segments(self, video_path: Path, segments: list[SilenceSegment]) -> None:
        self._gui._live_segments_store.set(video_path, segments)

    def pop_live_segments(self, video_path: Path) -> list[SilenceSegment] | None:
        return self._gui._live_segments_store.pop(video_path)


def main() -> None:
    app = Stream2VideoGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
