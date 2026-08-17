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
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from stream2video.config import effective_defaults
from stream2video.download import redact_input_url
from stream2video.gui_advanced import AdvancedSettingsMixin
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
    AdvancedSettingsMixin,
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
        self.settings = effective_defaults()

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
        # ``self.settings`` during construction).
        self._load_settings()
        ctk.set_appearance_mode(self.settings["theme"])

        # Fit window to screen if resolution is small
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        win_w, win_h = self._fit_to_screen(sw, sh)
        self.minsize(
            max(1, min(1000, sw - 40)),
            max(1, min(620, sh - 60)),
        )

        geom = self.settings.get("window_geometry")
        if geom:
            try:
                # Strict parse: reject malformed strings ("abcxdef",
                # "-100x50", empty pieces) up-front so we don't feed Tk
                # a geometry it'll reject with TclError mid-startup.
                import re as _re

                m = _re.fullmatch(r"(\d+)x(\d+)(?:[+-]\d+[+-]\d+)?", str(geom).strip())
                if not m:
                    raise ValueError(f"malformed geometry: {geom!r}")
                gw, gh = int(m.group(1)), int(m.group(2))
                if gw <= 0 or gh <= 0:
                    raise ValueError(f"non-positive dimension in geometry: {geom!r}")
                if gw <= sw and gh <= sh:
                    self.geometry(geom)
                else:
                    self.geometry(f"{win_w}x{win_h}")
            except Exception:
                logger.debug(f"window_geometry {geom!r} rejected; using default size")
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

        # The widgets were just populated from ``self.settings`` by
        # ``_build_ui``, and the widget values ARE the run's source of
        # truth (``_start_pipeline`` overlays the widget snapshot on the
        # config). Therefore the loaded values win as-is — do NOT replay
        # the preset over them at startup (audit round 13 P1: the old
        # ``_sync_preset_on_load`` re-applied the preset whenever the
        # stored values diverged from it, silently destroying a manual
        # override the user had made after selecting the preset). The
        # preset combo is display-only; selecting a preset pushes its
        # tunables into the widgets once (``_on_preset_change``), and any
        # later hand tweak to those widgets is the value that runs and
        # persists.

        # Wire the log queue → textbox poller once ``txt_log`` exists.
        # ``theme`` selects the warn/error tag colours for the log text.
        self._log_poller = LogQueuePoller(
            textbox=self.txt_log,
            dispatcher=self._dispatcher,
            log_queue=self.log_queue,
            theme=self.settings["theme"],
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
    # ``_start_pipeline`` reads widgets in the Tk main thread
    # and spawns ``_pipeline_worker`` on a background thread. The worker
    # builds a ``PipelineWorker`` (from ``pipeline_worker.py``) and
    # forwards an immutable ``PipelineWorkerParams`` snapshot; the
    # worker owns the PipelineController invocation, the callback
    # wiring, and the ``Pipeline*Error`` → status mapping.

    def _start_pipeline(self, dry_run: bool = False) -> None:
        """Start the pipeline on a background thread.

        ``dry_run=True`` (the "Dry run" button) stops the worker right
        after the silence pass: the controller reports which segments
        would be kept/cut without encoding anything (mirrors the CLI's
        ``--dry-run``).
        """
        if self.running:
            return

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self._log("[ERROR] ffmpeg/ffprobe not found in PATH")
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg and ffprobe are required to process video.\n\n"
                "Install: winget install Gyan.FFmpeg\n"
                "Or run: setup.ps1 (Windows)",
                # parent locks the dialog to our window so it can't fall
                # behind it (Windows + CTk child-window stacking is loose
                # for unparented messagebox calls, see the same fix in
                # gui_recent_projects.py).
                parent=self,
            )
            return

        # Validation gate (audit P2): reject invalid Advanced entries
        # up-front instead of running with a silent fallback while the
        # widget still shows the bad text. Mirrors the CLI resolver,
        # which rejects the same input with an explicit error.
        adv_errors = self._advanced_widget_errors(require_input=True)
        if adv_errors:
            for err in adv_errors.values():
                self._log(f"[ERROR] Invalid setting: {err}")
            messagebox.showerror(
                "Invalid settings",
                "Some Advanced settings are invalid:\n\n"
                + "\n".join(adv_errors.values())
                + "\n\nFix them and try again.",
                parent=self,
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
        # _pipeline_worker's signature).
        input_raw = self.entry_input.get().strip()
        if "~" in input_raw:
            input_raw = os.path.expanduser(input_raw)
        output_dir = Path(self.entry_output.get().strip() or "./processed_videos")
        # ``~`` expansion: Path.resolve() does NOT expand the tilde (it is
        # lexically relative to the CWD), so a ``~/Videos`` output path
        # used to silently create a ``~`` directory. Match
        # _copy_cli_command's expanduser() first.
        output_dir = Path(os.path.expanduser(str(output_dir))).resolve()
        # Read EVERY tunable through the shared helper — the same source
        # _copy_cli_command and _save_settings use, so the run, the
        # saved settings and the copied command can't disagree (the
        # audit found the copied command reading settings.json values
        # the run ignored). Previously only 6 keys were synced here and
        # the 18 advanced values silently fell back to stale
        # self.settings entries.
        widget_values = self._read_widget_values()
        method = widget_values["method"]
        encoder = widget_values["encoder"]
        video_quality = widget_values["video_quality"]
        audio_quality = widget_values["audio_quality"]
        download_quality = widget_values["download_quality"]
        force = widget_values["force"]
        per_video_dir = widget_values["per_video_dir"]
        delete_after = widget_values["delete_after"]

        # Build the run's config from the widget snapshot — the widgets
        # ARE the run's single source of truth (audit round 10: the
        # preset no longer overlays ``run_config`` at Start; selecting a
        # preset syncs its tunables INTO the managed widgets via
        # ``_on_preset_change``, so what the user sees is exactly what
        # runs and what the copied command pins). Re-applying the preset
        # here used to be overwritten by the widget snapshot anyway,
        # which is what made the selected preset a silent no-op while
        # the copied command's ``--preset`` ran differently (audit P1).
        preset_name = widget_values["preset"]
        run_config = dict(self.settings)
        run_config.update(widget_values)
        run_config["preset"] = preset_name

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
            is_local = (
                disk_input is not None
                and disk_input.exists()
                and disk_input.is_file()
                # A dry run never writes anything — skip the space
                # warning (it would claim "the run may fail" about a run
                # that cannot fail on disk space).
                and not dry_run
            )
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
                    if not messagebox.askokcancel(
                        "Low disk space — may fail", msg, icon="warning", parent=self
                    ):
                        self._log("Start cancelled — low disk space")
                        self._set_running(False)
                        return
        except Exception:
            # Whole pre-flight degraded to a no-op (ffprobe missing, a
            # units bug in the estimator, messagebox quirk): the feature
            # silently skipped — log at warning so it isn't invisible.
            logger.warning("start disk preflight skipped", exc_info=True)

        # Snapshot config for the worker thread: the
        # main thread keeps mutating ``self.settings`` as the user moves
        # sliders AFTER Start, and the worker reads ~30 keys off the
        # same dict — a race that produced mixed runs (new threshold,
        # old min_silence). ``run_config`` is already the preset-applied
        # copy; snapshot it so the worker sees a stable view.
        config_snapshot = dict(run_config)

        self._ui_update_output(output_dir)

        self._log(
            f"Starting pipeline: input={redact_input_url(input_raw)}, output_dir={output_dir}, "
            f"method={method}, encoder={encoder}, "
            f"video_quality={video_quality}, download_quality={download_quality}, "
            f"output_format={config_snapshot['output_format']}, "
            f"use_crf={config_snapshot['use_crf']}, "
            f"force={force}, "
            f"threshold={config_snapshot['threshold']}, "
            f"min_silence={config_snapshot['min_silence']}, "
            f"margin={config_snapshot['margin']}, "
            f"preset={config_snapshot.get('preset')}, "
            f"delete_after={delete_after}, "
            f"per_video_dir={per_video_dir}"
            f"{', DRY RUN — no encode' if dry_run else ''}"
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
                config_snapshot,
                dry_run,
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
        config_snapshot: dict | None = None,
        dry_run: bool = False,
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
             widget directly.
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
            dry_run=dry_run,
        )
        worker = PipelineWorker(
            _PipelineGuiCallbacksAdapter(self),
            config_snapshot if config_snapshot is not None else self.settings,
        )
        self._pipeline_start = time.monotonic()
        try:
            worker.run(params)
        finally:
            # The worker owns the real resolved paths (controller-level
            # ``_output_path`` / ``_download_path``); nothing to clear
            # here — a stale GUI copy would just lie about the last run.
            pass


class _GuiLogAdapterBase:
    """Shared ``log`` plumbing for adapters that forward the GUI's log.

    Every adapter whose Protocol requires a ``log(message)`` method
    funnels it through the GUI's ``_log`` — the implementation was
    previously copy-pasted across ``_EncoderTesterAdapter`` and
    ``_PipelineGuiCallbacksAdapter``. Both keep their own ``__init__``
    (they bind extra state); only the log path is shared.
    """

    def __init__(self, gui: Any) -> None:
        self._gui = gui

    def log(self, message: str) -> None:
        self._gui._log(message)


class _EncoderTesterAdapter(_GuiLogAdapterBase):
    """Adapter that exposes the GUI's encoder-test surface to
    :class:`stream2video.encoder_test.EncoderTester`.
    """

    def schedule_on_main(self, ms: int, func: Callable[..., Any]) -> None:
        # ``_tk_after`` swallows ``TclError`` if the root is destroyed
        # mid-test — exactly what the legacy code did. Note this is the
        # ONLY scheduler in the interface: the legacy ``schedule_after``
        # was byte-identical and the worker (EncoderTester) always
        # invokes it cross-thread, so ``self.after`` would race window
        # teardown; both names now resolve to this one method.
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


class _PipelineGuiCallbacksAdapter(_GuiLogAdapterBase):
    """Adapter exposing the GUI's pipeline-run surface to
    :class:`stream2video.pipeline_worker.PipelineWorker`.

    The GUI can't be passed to the worker directly (it's the fat class
    at the bottom of this module; we want the worker module to depend
    only on a thin Protocol callable surface). So the GUI hands the
    worker a tiny adapter object that funnels every call back through
    the GUI's main-thread dispatcher (``_tk_after``) — the worker never
    touches a widget directly.

    ``cancel_event`` is just the GUI's own ``self._cancel_event`` — the
    worker reads it directly so the existing ``_cancel_pipeline`` path
    keeps working without going through a dispatcher.
    """

    def __init__(self, gui: Any):
        super().__init__(gui)
        # Protocol-expected settable attribute; expose the GUI's event
        # so the worker (and the controller it instantiates) can poll
        # it without going through a dispatcher.
        self.cancel_event = gui._cancel_event

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
        if consent[0]:
            # The on-screen encoder label still shows the *requested*
            # encoder; a "yes" here silently swaps to libx264 below the
            # surface. Refresh it so the UI reflects what's actually
            # running.
            try:
                self._gui._tk_after(
                    0,
                    lambda: self._gui.lbl_encoder.configure(text="Encoder: libx264 (fallback)"),
                )
            except Exception:
                pass
        return consent[0]

    def ask_legacy_rename(self, legacy: Path, target: Path) -> bool:
        """Block the worker while the user answers a yes/no dialog on
        the Tk main loop: a legacy (pre-namespace) project directory
        was found — offer an opt-in rename so the old multi-GB source
        and caches are reused instead of re-downloaded (audit round
        28 P9). Same bridge pattern as ``ask_fallback_consent``: any
        dialog error or timeout defaults to False (no rename — the old
        dir is simply left alone).
        """
        answered = threading.Event()
        consent: list[bool] = [False]

        def _ask() -> None:
            try:
                consent[0] = bool(
                    messagebox.askyesno(
                        "Legacy project directory found",
                        "An older stream2video version stored this video's "
                        f"project under:\n{legacy}\n\nThe current version uses:\n"
                        f"{target}\n\nRename the old directory so its files "
                        "(source, caches) are reused instead of re-downloaded?",
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
        if not answered.wait(timeout=60):
            self._gui._log("[WARN] Legacy-project dialog timed out — keeping the old directory")
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
        # parent=self._gui keeps the completion dialog on top of the
        # main window — an unparented dialog can fall behind it on
        # Windows and look like the app froze before any click passed.
        self._gui._tk_after(0, lambda: messagebox.showinfo("Complete", text, parent=self._gui))

    def set_running(self, running: bool) -> None:
        self._gui._tk_after(0, lambda: self._gui._set_running(running))

    def set_live_segments(self, run_id: int, segments: list[SilenceSegment]) -> None:
        self._gui._live_segments_store.set(run_id, segments)

    def pop_live_segments(self, run_id: int | None = None) -> None:
        self._gui._live_segments_store.clear(run_id)

    def current_live_run_id(self) -> int | None:
        return self._gui._live_segments_store.current_run_id()

    def set_active_controller(self, controller: object) -> None:
        self._gui._active_controller = controller

    def clear_active_controller(self) -> None:
        self._gui._active_controller = None

    def begin_live_segments_run(self) -> int:
        """Allocate the run_id the worker will publish live segments under."""
        return self._gui._live_segments_store.begin_run()


def main() -> None:
    app = Stream2VideoGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
