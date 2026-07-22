"""Background pipeline orchestration for the GUI — extracted from
``gui.py`` (Этап 10 incremental refactor).

The GUI's ``_pipeline_worker`` worker thread did three things inline:
  1. Built the immutable :class:`stream2video.pipeline_controller.PipelineConfig`
     from the GUI's config dict + the widget-snapshot arguments.
  2. Built the four ``PipelineCallbacks`` closures that route
     download-progress / live-segment / output-resolved / completion
     events back through the Tk main-loop dispatcher.
  3. Ran :class:`PipelineController` and mapped ``Pipeline*Error``
     subclasses to status lines.

All of that is now:
  * :func:`build_pipeline_config_from_snapshot` — pure factory that
    turns a config dict + the worker-args into a ``PipelineConfig``.
    Unit-tested: the test suite pins the field map and the default
    fallbacks (e.g. ``memory_limit_mb='auto'`` when absent) without
    instantiating the GUI.
  * :class:`PipelineGuiCallbacks` — a Protocol the GUI implements with
    its ``_log`` / ``_ui_status`` / ``_ui_progress`` / ``_ui_overall``
    / ``_ui_total`` / ``_ui_info`` methods plus three GUI-specific
    hooks (``add_to_recent_projects``, ``update_output_label``,
    ``update_file_info``, ``set_encoder_label``, ``show_complete_popup``,
    ``pop_live_segments``). Exported as a Protocol so the test suite
    can build a fake without inheriting from the GUI.
  * :class:`PipelineWorker` — drives a pipeline run: builds the
    callbacks, instantiates the controller, runs it, maps exceptions
    to status, restores button state via the dispatcher. The GUI holds
    a single instance, calls ``.run(snapshot)`` from a worker thread,
    and lets the worker own the orchestration.

The GUI's own ``_pipeline_worker`` becomes a tiny adapter:
  1. Read widgets in the Tk main thread (P1.10) into a
     ``PipelineWorkerParams`` dataclass.
  2. Spawn a worker thread that calls ``PipelineWorker.run(snapshot)``.
The orchestration (PipelineController → callback mapping → exception
handling) lives here so a unit test can drive it without Tk.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from stream2video.download import DownloadProgress
from stream2video.formatters import fmt_size, fmt_speed, fmt_time
from stream2video.silence import SilenceSegment

logger = logging.getLogger("stream2video.pipeline_worker")


@dataclass(frozen=True)
class PipelineWorkerParams:
    """Widget snapshot taken in the Tk main thread and forwarded to
    the worker — the worker is forbidden from touching Tk widgets
    directly (P1.10 in the fix plan).

    Mirrors the positional args the GUI's ``_pipeline_worker`` method
    accepted, plus the GUI's config dict reference (only the slider
    / preset / timeout values are read; the worker never writes).
    """

    input_raw: str
    output_dir: Path
    method: str
    encoder: str
    video_quality: str
    audio_quality: str
    download_quality: str
    force: bool
    per_video_dir: bool = False
    delete_after: bool = False


class PipelineGuiCallbacks(Protocol):
    """Tiny interface the GUI implements for :class:`PipelineWorker`.

    Centralised as a Protocol (structural typing) so the test suite can
    build a fake with the methods and pass it to :class:`PipelineWorker`
    without inheriting from the GUI. Every method on this Protocol is
    safe to call from a worker thread: the GUI's implementations all
    dispatch to the Tk main loop via ``self._tk_after`` / ``self.after``
    so widgets are only touched on the main thread.
    """

    def log(self, message: str) -> None: ...

    def ui_progress(self, value: float) -> None: ...

    def ui_status(self, text: str, *, force: bool = ...) -> None: ...

    def ui_info(self, text: str) -> None: ...

    def ui_overall(
        self, phase_elapsed: float, phase_remaining: float | None, more_phases: bool
    ) -> None: ...

    def ui_total(self, total_elapsed: float) -> None: ...

    def ui_update_output(self, out_dir: Path) -> None: ...

    def ui_update_file_info(self, path: Path) -> None: ...

    def add_to_recent_projects(self, project_path: Path) -> None: ...

    def set_encoder_label(self, encoder: str, video_quality: str) -> None: ...

    def clear_overall_label(self) -> None: ...

    def show_complete_popup(self, text: str) -> None: ...

    def set_running(self, running: bool) -> None: ...

    # Live-segments store the worker threads data into.
    def set_live_segments(self, video_path: Path, segments: list[SilenceSegment]) -> None: ...

    def pop_live_segments(self, video_path: Path) -> list[SilenceSegment] | None: ...

    # ``cancel_event`` is the GUI's threading.Event the controller
    # checks after each phase; the worker reads it through the GUI
    # callbacks object so the test fake can return a fresh event. Plain
    # instance attribute (NOT a Protocol property — a plain ``Event``
    # object the controller polls).
    cancel_event: threading.Event


def build_pipeline_config_from_snapshot(params: PipelineWorkerParams, config: dict[str, Any]):
    """Factory: build a :class:`stream2video.pipeline_controller.PipelineConfig`
    from the GUI's config dict + the widget-snapshot params.

    Reading every key with ``config.get(key, default)`` mirrors the GUI's
    original inline behaviour: a missing key falls back to the same
    default the PipelineConfig expects. Pure (no widget reads, no I/O);
    the returned dataclass is frozen so the worker can't mutate it
    mid-run.

    Delay-imports ``PipelineController`` so the GUI module load never
    pulls the pipeline (and its ffmpeg-dependent helpers) ahead of
    schedule — the GUI imports stay shallow.
    """
    from stream2video.pipeline_controller import PipelineConfig

    return PipelineConfig(
        input_raw=params.input_raw,
        output_dir=params.output_dir,
        method=params.method,
        encoder=params.encoder,
        video_quality=params.video_quality,
        audio_quality=params.audio_quality,
        download_quality=params.download_quality,
        software_fallback=config.get("software_fallback", "ask"),
        x264_preset=config.get("x264_preset", "medium"),
        encoder_threads=config.get("encoder_threads", "auto"),
        output_fps=config.get("output_fps", "source"),
        force=params.force,
        delete_after=params.delete_after,
        per_video_dir=params.per_video_dir,
        threshold=float(config["threshold"]),
        min_silence=float(config["min_silence"]),
        margin=float(config["margin"]),
        memory_limit_mb=config.get("memory_limit_mb", "auto"),
        memory_reserve_mb=config.get("memory_reserve_mb", 2048),
        x264_low_memory=config.get("x264_low_memory", False),
        download_timeout=config.get("download_timeout", 28800),
        connect_timeout=config.get("connect_timeout", 300),
        no_progress_timeout=config.get("no_progress_timeout", 1800),
        segment_encode_timeout=config.get("segment_encode_timeout", 600),
        final_concat_timeout=config.get("final_concat_timeout", 86400),
        silence_timeout=config.get("silence_timeout", 36000),
        stall_kill_timeout=config.get("stall_kill_timeout", 300),
        stall_warning_timeout=config.get("stall_warning_timeout", 120),
        waveform_timeout=config.get("waveform_timeout", 300),
        batch_chunk_size=config.get("batch_chunk_size", 40),
        min_part_bytes=config.get("min_part_bytes", 1024),
    )


def build_download_progress_callback(
    gui: PipelineGuiCallbacks, start_monotonic: float
) -> Callable[[DownloadProgress], None]:
    """Factory: build the ``on_download_progress`` callback the
    PipelineCallbacks expect, mapping yt-dlp's
    :class:`~stream2video.download.DownloadProgress` to the GUI's overall
    bar (0..5%) and a status string with percent + size + speed + ETA.

    Pure-ish: returns a closure; doesn't itself read Tk widgets — it
    delegates through the callbacks Protocol's ``ui_*`` methods so the
    test can pass a fake.

    Extracted from the GUI's inline closure so the percentage math can
    be unit-tested against a synthetic ``DownloadProgress``.
    """
    download_start = start_monotonic

    def _on_download_progress(p: DownloadProgress) -> None:
        elapsed = time.monotonic() - download_start
        if p.total_bytes and p.total_bytes > 0:
            frac = min(1.0, (p.downloaded_bytes or 0.0) / p.total_bytes)
            gui.ui_progress(0.05 * frac)
        else:
            gui.ui_progress(min(0.04, 0.005 * elapsed))
        pct = 100.0 * (p.downloaded_bytes or 0.0) / p.total_bytes if p.total_bytes else 0.0
        gui.ui_status(
            f"Step 1/3: Downloading {pct:.0f}% "
            f"({fmt_size(int(p.downloaded_bytes or 0))}/{fmt_size(int(p.total_bytes or 0))}) "
            f"at {fmt_speed(p.speed)} ETA {fmt_time(p.eta) if p.eta else '?'}",
            force=True,
        )
        gui.ui_overall(elapsed, p.eta or 0.0, True)

    return _on_download_progress


def build_completion_callback(gui: PipelineGuiCallbacks) -> Callable[[dict], None]:
    """Factory: build the ``on_pipeline_complete`` callback the
    PipelineCallbacks expect, mapping the controller's summary dict to
    a status line + log lines + a "Complete" popup.

    Imports ``build_completion_summary`` lazily so the helper (and its
    transitive imports) load only when a pipeline actually completes —
    the GUI module stays shallow.
    """
    from stream2video.gui_helpers import build_completion_summary

    def _on_pipeline_complete(summary: dict) -> None:
        result = build_completion_summary(
            src_size_bytes=summary["src_size_bytes"],
            src_duration=summary["src_duration"],
            dst_size_bytes=summary["dst_size_bytes"],
            dst_duration=summary["keep_duration"],
            pipeline_seconds=summary["pipeline_seconds"],
            output_path=summary["output_path"],
        )
        gui.ui_status(result["status"], force=True)
        for line in result["log_lines"]:
            gui.log(line)
        gui.clear_overall_label()
        gui.ui_total(summary["pipeline_seconds"])
        gui.show_complete_popup(result["popup"])

    return _on_pipeline_complete


class PipelineWorker:
    """Drives a single pipeline run on a worker thread.

    The GUI holds one instance; ``run()`` is called once per "Start"
    click with a pattern like::

        thread = threading.Thread(target=worker.run, args=(params,), daemon=True)
        thread.start()

    Exception handling walks the ``Pipeline*Error`` inheritance chain
    and maps each to the appropriate log line + status text. The
    GUI's button state is restored via ``set_running(False)`` in
    ``finally`` — the GUI's implementation dispatches to the Tk main
    loop so the call is safe from the worker thread.
    """

    def __init__(self, gui: PipelineGuiCallbacks, config: dict[str, Any]):
        self._gui = gui
        self._config = config

    def run(self, params: PipelineWorkerParams) -> None:
        from stream2video.pipeline_controller import (
            PipelineCallbacks,
            PipelineCancelled,
            PipelineConcatError,
            PipelineController,
            PipelineDownloadError,
            PipelineSilenceError,
            PipelineUnexpectedError,
        )

        cfg = build_pipeline_config_from_snapshot(params, self._config)

        # Mutable holder for the resolved video path — the
        # ``on_output_resolved`` callback fills it and ``_on_live_segment``
        # / the success cleanup read it after.
        video_path_ref: list[Path] = [Path()]
        download_start = time.monotonic()

        def _on_live_segment(seg_list: list[SilenceSegment]) -> None:
            self._gui.set_live_segments(video_path_ref[0], list(seg_list))

        def _on_output_resolved(out_dir: Path, vpath: Path, is_dl: bool) -> None:
            video_path_ref[0] = vpath
            self._gui.add_to_recent_projects(out_dir)
            self._gui.ui_update_output(out_dir)
            self._gui.ui_update_file_info(vpath)
            self._gui.log(f"Project directory: {out_dir}")
            self._gui.set_encoder_label(params.encoder, params.video_quality)

        cb = PipelineCallbacks(
            on_progress=self._gui.ui_progress,
            on_status=self._gui.ui_status,
            on_log=self._gui.log,
            on_info=self._gui.ui_info,
            on_overall=self._gui.ui_overall,
            on_total=self._gui.ui_total,
            on_download_progress=build_download_progress_callback(self._gui, download_start),
            on_pipeline_complete=build_completion_callback(self._gui),
        )

        controller = PipelineController(
            cfg=cfg,
            cb=cb,
            cancel_event=self._gui.cancel_event,
            on_live_segment=_on_live_segment,
            on_output_resolved=_on_output_resolved,
        )

        try:
            controller.run()
            if video_path_ref[0]:
                self._gui.pop_live_segments(video_path_ref[0])
            if params.delete_after and controller._download_path is not None:
                try:
                    controller._download_path.unlink()
                    self._gui.log(f"Deleted source: {controller._download_path}")
                except OSError as e:
                    self._gui.log(f"[WARN] Could not delete source: {e}")
        except PipelineCancelled:
            self._gui.log("Pipeline cancelled")
            self._gui.ui_status("Cancelled", force=True)
        except PipelineDownloadError as e:
            self._gui.log(f"[ERROR] Download failed: {e}")
            self._gui.ui_status(f"Failed: {e}", force=True)
        except PipelineSilenceError as e:
            self._gui.log(f"[ERROR] Silence detection failed: {e}")
            self._gui.ui_status(f"Failed: {e}", force=True)
        except PipelineConcatError as e:
            self._gui.log(f"[ERROR] {e}")
            self._gui.ui_status(f"Failed: {e}", force=True)
        except PipelineUnexpectedError as e:
            self._gui.log(f"[ERROR] Unexpected: {e}")
            logger.exception("Pipeline error")
            self._gui.ui_status(f"Error: {e}", force=True)
        finally:
            self._gui.set_running(False)
