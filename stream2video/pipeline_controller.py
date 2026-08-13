"""Pipeline controller extracted from ``gui.py`` (Этап 10 incremental).

The pipeline worker in ``gui._pipeline_worker`` is a ~350-line method
that interleaves:
  * pure pipeline orchestration (download → silence → cut+concat)
  * Tk widget updates (progress bar, status label, log textbox)
  * cancel / error handling
  * per-step progress mapping (download / silence / cut+concat)

This module defines:
  * ``PipelineConfig`` — immutable snapshot of the 22 inputs.
  * ``PipelineCallbacks`` — bundle of 8 callables for progress/status.
  * ``PipelineResult`` — what ``run()`` returns on success.
  * ``PipelineController`` — orchestrator with ``run()`` that drives
    download → silence → cut+concat through the callbacks. No Tk;
    the GUI passes bound methods that dispatch to the Tk main loop.

The controller is unit-testable with mock callbacks + monkeypatched
``download`` / ``detect_silence`` / ``cut_and_concat`` — no ffmpeg
needed for the orchestration tests (media correctness is covered
separately by ``tests/test_media_correctness.py``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from stream2video.concat import (
    CancelledError,
    ConcatError,
    cut_and_concat,
    generate_keep_segments,
)
from stream2video.config import VALID_QUALITIES
from stream2video.download import (
    DiskSpaceError,
    DownloadCancelledError,
    DownloadError,
    DownloadProgress,
    DownloadTimeoutError,
    FileBusyError,
    PermissionDeniedError,
    URLValidationError,
    VideoNotAvailableError,
    download,
)
from stream2video.formatters import fmt_size, fmt_time
from stream2video.gui_helpers import build_silence_info_line
from stream2video.memory import check_memory_reserve
from stream2video.paths import apply_per_video_dir
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceSegment,
    build_resume_cache_path,
    detect_silence,
    load_silence_cache,
    resume_inuse_path,
    save_silence_cache,
)
from stream2video.utils import check_disk_space as _check_disk_space
from stream2video.utils import get_video_duration

logger = logging.getLogger(__name__)

_MEMORY_POLL_INTERVAL = 2.0


def _unlink_with_retry(path: Path, attempts: int = 5, delay_s: float = 0.2) -> bool:
    """Best-effort unlink with short retries for Windows AV/indexer locks.

    A fresh download or a just-finished output is often held open by a
    virus scanner or Windows Search indexer for tens of milliseconds
    (WinError 32). A bare ``unlink()`` then fails and the cleanup helper
    leaves garbage on disk — exactly the "failed run leaves a stale file"
    bug the cleanup was designed to prevent (fix-plan #22). Retries with
    a short sleep absorb the transient lock; a final failure is logged
    with the full path so the user can delete it manually.
    """
    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True  # already gone — cleanup goal achieved
        except OSError as e:
            if attempt < attempts - 1:
                time.sleep(delay_s)
            else:
                logger.warning("_unlink_with_retry(%s): %s after %d attempts", path, e, attempts)
    return False


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable snapshot of the pipeline inputs.

    The GUI snapshots widget values into this dataclass in the main
    thread (Tk widgets are not thread-safe for cross-thread access —
    see P1.10) and passes it to the worker thread. The worker reads
    only these local copies, so a concurrent slider / settings change
    doesn't race with the running pipeline.
    """

    input_raw: str
    output_dir: Path
    method: str
    encoder: str
    video_quality: str
    audio_quality: str
    download_quality: str
    software_fallback: str
    x264_preset: str
    encoder_threads: str | int
    output_fps: str
    # Output container/codec policy. ``video`` preserves the historical
    # H.264 + AAC MP4 behaviour; the other values (``mp3`` / ``opus`` /
    # ``aac`` / ``wav`` / ``flac``) produce a standalone audio file
    # (video stream dropped). See OUTPUT_FORMAT_SPECS in config.py.
    output_format: str
    force: bool
    delete_after: bool
    per_video_dir: bool
    threshold: float
    min_silence: float
    margin: float
    memory_limit_mb: str | int
    memory_reserve_mb: int
    x264_low_memory: bool
    # Quality-fixed mode for video encoders. libx264 uses CRF, NVENC/AMF
    # use CQ/QP-style modes, and MF uses quality mode. Default False keeps
    # bitrate parity across encoders.
    use_crf: bool

    # Gapless concat (AAC priming fix). When True, the segment path's
    # final join uses the ``concat`` filter (re-encode) instead of the
    # concat demuxer (stream copy) so per-segment AAC priming doesn't
    # accumulate as A/V drift on multi-segment outputs. Default False
    # preserves the historical behaviour (concat demuxer, faster).
    gapless_concat: bool
    # Lower ffmpeg scheduling priority (opt-in, P3.x). When True,
    # spawned ffmpeg subprocesses use BELOW_NORMAL_PRIORITY_CLASS on
    # Windows and nice +10 on POSIX so a long encode doesn't starve
    # interactive applications. See subprocess_kwargs in utils.py.
    low_process_priority: bool
    # RLIMIT_AS cap for ffmpeg subprocesses (POSIX-only, opt-in, P3.x).
    # When > 0, the child is forked with resource.setrlimit(RLIMIT_AS,
    # (cap, cap)) so it cannot allocate more than this MiB of virtual
    # address space. No-op on Windows. See subprocess_kwargs in utils.py.
    rlimit_as_mb: int
    download_timeout: int
    connect_timeout: int
    no_progress_timeout: int
    # Proxy server used for the download phase ("http://host:port",
    # "socks5://..."). Empty string = direct connection. Passed to
    # yt-dlp as --proxy; ignored for local files.
    proxy: str = ""
    # Pipeline phase timeouts + tuning (P3.4). Plumbed into
    # detect_silence / cut_and_concat / read_peaks_from_stream; module-
    # level constants in concat.py / silence.py / waveform.py remain
    # as fallbacks for direct callers that don't pass config values.
    segment_encode_timeout: int = 600
    final_concat_timeout: int = 86400
    silence_timeout: int = 36000
    stall_kill_timeout: int = 300
    stall_warning_timeout: int = 120
    waveform_timeout: int = 300
    batch_chunk_size: int = 40
    min_part_bytes: int = 1024


@dataclass(frozen=True)
class PipelineCallbacks:
    """Callback bundle the controller uses to report progress / status.

    Each callback runs on the WORKER thread; the GUI's implementations
    schedule the actual Tk widget update on the main loop via
    ``self._tk_after(0, lambda: ...)``. Keeping them as plain
    ``Callable``s (not a Protocol) lets the GUI pass bound methods
    directly.
    """

    on_progress: Callable[[float], None]
    # ``force=True`` bypasses the GUI's status throttling for step-boundary
    # / error updates. Keyword-style so hosts can declare
    # ``def cb(text, *, force=False)``; a plain two-positional closure
    # would also type-check here but the callers below pass the flag as
    # a keyword, so the annotation must include the keyword form.
    on_status: Callable[..., None]
    on_log: Callable[[str], None]
    on_info: Callable[[str], None]
    on_overall: Callable[[float, float | None, bool], None]
    on_total: Callable[[float], None]
    on_download_progress: Callable[[DownloadProgress], None]
    on_pipeline_complete: Callable[[dict], None]
    # Fraction (0..1) within the CURRENT phase — drives the thin
    # per-phase bar under the log. Optional so existing callers (GUI
    # smoke tests, the CLI's silent-callback bundle) keep working
    # without change; a None-able ``lambda f: None`` would also work but
    # an Optional avoids a stray no-op closure in the default case.
    on_phase_progress: Callable[[float], None] | None = None
    # The per-run ``ProgressPlan`` boundary fractions — the tuple
    # ``(download_end, silence_end, cut_end, concat_end)`` in overall
    # 0..1 space. Sent on EVERY plan (re)build so the GUI can draw
    # phase tick marks on the overall bar that match the adaptive
    # weights (local files / cached silence shrink cheap spans). The
    # GUI converts these to per-phase weight percents itself; the CLI
    # and tests that don't draw segments simply omit the callback.
    on_progress_plan: Callable[[tuple[float, float, float, float]], None] | None = None


@dataclass(frozen=True)
class PipelineResult:
    """What ``PipelineController.run()`` returns on success.

    Failure paths raise ``PipelineError`` (or its subclasses), so the
    return value is only for the success case — the GUI's worker uses
    it to drive the completion popup + log block.
    """

    output_path: Path
    video_path: Path
    src_size_bytes: int
    src_duration: float | None
    dst_size_bytes: int
    keep_duration: float
    pipeline_seconds: float


class PipelineError(Exception):
    """Base error for ``PipelineController.run()`` failures."""


class PipelineCancelled(PipelineError):
    """User cancelled the pipeline (cancel_event set).

    ``partial`` carries the download-phase signal through to ``run()``'s
    cleanup: ``partial=True`` means the cancel fired while yt-dlp was
    still writing the source file (so the on-disk bytes are truncated
    and safe to unlink); ``False`` means the source was already fully
    downloaded and should be kept for the user's next retry. Post-
    download cancels always set ``partial=False``.
    """

    def __init__(self, message: str = "Pipeline cancelled", *, partial: bool = False) -> None:
        super().__init__(message)
        self.partial = partial


class PipelineDownloadError(PipelineError):
    """Download phase failed (network / disk / permission / unavailable)."""


class PipelineSilenceError(PipelineError):
    """Silence detection phase failed."""


class PipelineConcatError(PipelineError):
    """Cut+concat phase failed."""


class PipelineUnexpectedError(PipelineError):
    """Any other exception not mapped to a specific phase."""


# Default progress-bar fractions. Individual runs may shrink cheap
# phases (local input / silence cache) and give the remaining span to
# cut+concat, but these values remain the conservative fallback profile.
# Cutting gets 90% of the concat span (per-segment encodes), concatenating
# 10% (final join / gapless tree) — mirrors the 0.9 split in
# stream2video.concat.segment._run_segment_concat.
PROG_DOWNLOAD_END = 0.05
PROG_SILENCE_END = 0.40
PROG_CUT_END = 0.94
PROG_CONCAT_END = 1.00


@dataclass(frozen=True)
class ProgressPlan:
    """Per-run overall progress mapping for the heavy phases.

    Historical three-phase view (download / silence / concat) is kept via
    ``weights_percent`` for backward compat, but the concat span is now
    exposed atomically as cutting (0..0.9) + concatenating (0.9..1.0).
    """

    download_end: float = PROG_DOWNLOAD_END
    silence_end: float = PROG_SILENCE_END
    cut_end: float = PROG_CUT_END
    concat_end: float = PROG_CONCAT_END

    @property
    def download_span(self) -> float:
        return self.download_end

    @property
    def silence_span(self) -> float:
        return self.silence_end - self.download_end

    @property
    def cut_span(self) -> float:
        return self.cut_end - self.silence_end

    @property
    def concat_span(self) -> float:
        return self.concat_end - self.cut_end

    @property
    def total_concat_span(self) -> float:
        return self.concat_end - self.silence_end

    def map_silence(self, fraction: float) -> float:
        return self.download_end + _clamp_fraction(fraction) * self.silence_span

    def map_cut(self, fraction: float) -> float:
        return self.silence_end + _clamp_fraction(fraction) * self.cut_span

    def map_concat(self, fraction: float) -> float:
        return self.cut_end + _clamp_fraction(fraction) * self.concat_span

    def weights_percent(self) -> tuple[int, int, int, int]:
        return (
            round(self.download_span * 100),
            round(self.silence_span * 100),
            round(self.cut_span * 100),
            round(self.concat_span * 100),
        )

    def weights_percent_legacy(self) -> tuple[int, int, int]:
        return (
            round(self.download_span * 100),
            round(self.silence_span * 100),
            round(self.total_concat_span * 100),
        )


def _clamp_fraction(value: float) -> float:
    return max(0.0, min(1.0, value))


def _estimate_silence_span(src_duration: float | None, *, cache_hit: bool) -> float:
    if cache_hit:
        return 0.10
    if src_duration is None or src_duration <= 0:
        return PROG_SILENCE_END - PROG_DOWNLOAD_END
    if src_duration <= 10 * 60:
        return 0.25
    if src_duration <= 60 * 60:
        return 0.30
    return PROG_SILENCE_END - PROG_DOWNLOAD_END


def _build_progress_plan(
    *,
    is_downloaded: bool,
    src_duration: float | None,
    silence_cache_hit: bool,
) -> ProgressPlan:
    download_end = 0.0 if not is_downloaded else PROG_DOWNLOAD_END
    silence_span = _estimate_silence_span(src_duration, cache_hit=silence_cache_hit)
    silence_end = min(0.75, download_end + silence_span)
    # Cutting gets 90% of the remaining span (mirrors 0.9 in segment/batch),
    # concatenating the final 10% (gapless tree / concat demuxer).
    remaining = max(0.0, PROG_CONCAT_END - silence_end)
    cut_end = silence_end + remaining * 0.9
    return ProgressPlan(download_end=download_end, silence_end=silence_end, cut_end=cut_end)


@dataclass
class PipelineController:
    """Orchestrates the download → silence → cut+concat pipeline.

    Pure-Python (no Tk) so it can be unit-tested with mock callbacks
    + monkeypatched ``download`` / ``detect_silence`` / ``cut_and_concat``.
    The GUI constructs the controller per run with the user's
    ``PipelineConfig`` and a ``PipelineCallbacks`` bundle whose
    callables dispatch to the Tk main loop.

    Single-use: not designed to be reused across runs (state like
    ``_download_path`` accumulates); construct a fresh controller for
    each pipeline invocation.
    """

    cfg: PipelineConfig
    cb: PipelineCallbacks
    cancel_event: threading.Event
    # ``on_live_segment`` is the callback ``detect_silence`` invokes
    # with the running segment list so the GUI's waveform popup updates
    # in near real-time. Optional because the CLI doesn't need it.
    on_live_segment: Callable[[list[SilenceSegment]], None] | None = None
    # ``on_output_resolved`` fires after the download phase completes
    # and ``apply_per_video_dir`` has resolved the final output dir.
    # The GUI uses it to add the project to the recent-projects panel,
    # update the output label, and refresh the file-info panel — all
    # main-thread operations the worker thread defers to this hook.
    on_output_resolved: Callable[[Path, Path, bool], None] | None = None
    # ``on_fallback_consent`` implements ``software_fallback="ask"`` for
    # interactive hosts. The pipeline controller's fresh-encoder check
    # and mid-run fallback both call it when the requested encoder is
    # missing / fails; the GUI pops a yes/no dialog on the main thread
    # and the CLI wires ``typer.confirm``. When None (default) and the
    # policy is ``ask``, the pipeline raises — ``ask`` without a consent
    # handler must never silently switch encoders.
    on_fallback_consent: Callable[[], bool] | None = None

    # Mutable per-run state. The GUI reads these after ``run()`` to
    # drive the completion summary + recent-projects panel; on error
    # they're still populated up to the point where the failure occurred.
    _download_path: Path | None = field(default=None, init=False)
    _output_path: Path | None = field(default=None, init=False)
    # Latched True once the download phase returns with a file on disk
    # (``download_result.is_downloaded``). From that point the source is
    # a fully-downloaded user asset; a subsequent cancel/error in the
    # silence/concat phases must NOT unlink it — the cost of re-fetching
    # a multi-GB VOD dwarfs the disk cost of keeping it. ``False``
    # throughout a local-file run and until the download phase returns.
    _download_complete: bool = field(default=False, init=False)
    # Resolved per-run output directory (per_video_dir project dir when
    # enabled, else cfg.output_dir). Declared explicitly instead of being
    # set ad-hoc in _run_download_phase and read back via getattr — the
    # getattr fallback silently returned cfg.output_dir when the field had
    # never been written, hiding a real "phase order" bug behind a wrong
    # default.
    _output_dir_resolved: Path | None = field(default=None, init=False)
    _pipeline_start: float = field(default=0.0, init=False)
    _progress_plan: ProgressPlan = field(default_factory=ProgressPlan, init=False)
    _download_was_real: bool = field(default=True, init=False)
    _src_duration: float | None = field(default=None, init=False)

    def _set_status(self, text: str, *, force: bool = False) -> None:
        self.cb.on_status(text, force=force)

    def _set_phase_progress(self, fraction: float) -> None:
        """Dispatch the per-phase bar update (no-op when the callback
        isn't wired — CLI / tests). Frac clamped to [0, 1]."""
        if self.cb.on_phase_progress is not None:
            self.cb.on_phase_progress(max(0.0, min(1.0, fraction)))

    def _emit_progress_plan(self) -> None:
        """Broadcast the current plan's phase boundaries in overall
        0..1 space so the GUI can draw adaptive segment tick marks."""
        if self.cb.on_progress_plan is not None:
            plan = self._progress_plan
            self.cb.on_progress_plan(
                (plan.download_end, plan.silence_end, plan.cut_end, plan.concat_end)
            )

    def _cleanup_download_path(self, *, partial_only: bool = False) -> None:
        """Remove the downloaded source file (when we downloaded one).

        ``_download_path`` is only set when the download phase wrote a
        fresh file (``download_result.is_downloaded``). For local files
        it's always None, so this is a no-op — the user's local file is
        never touched (ownership check).

        When ``partial_only`` is True, only files that are known-truncated
        are removed: a user Ctrl+C or a stall/watchdog kill mid-pipeline
        must not nuke a fully-downloaded 15 GB source the user may want
        to reuse on the next run (the download is over by then, and the
        cost of re-fetching dwarfs the cost of short-term disk use).
        ``partial_only`` is therefore the right mode for the post-download
        phases; the download phase itself, which calls this before any
        later phase runs, omits the flag and still cleans up a genuine
        partial byte-sink.
        """
        if self._download_path is not None and self._download_path.exists():
            if partial_only and (self._download_complete or self._download_was_real):
                self.cb.on_log(
                    f"Keeping completed download for possible reuse: {self._download_path}"
                )
            else:
                _unlink_with_retry(self._download_path)
        self._download_path = None

    def _cleanup_partial_output(self) -> None:
        """Remove a partially-written output file on failure/cancel.

        ``_output_path`` is stamped by ``_run_concat_phase`` before the
        cut+concat subprocess runs, so an exception (``PipelineConcatError``,
        ``PipelineCancelled``, or an unexpected crash) leaves a partially
        muxed ``*_compressed.*`` file on disk that looks like a completed
        output (a bare ``ffmpeg -i`` inside the concat step writes the
        container header at t=0 — the file plays, but it's truncated
        mid-stream). Deleting it here catches the same file the GUI's
        on-close cleanup never reaches, but located in the controller
        (the only place that actually knows the resolved path).
        """
        if self._output_path is not None and self._output_path.exists():
            if _unlink_with_retry(self._output_path):
                self.cb.on_log(f"Deleted incomplete output: {self._output_path}")
            else:
                self.cb.on_log(f"[WARN] Could not delete incomplete output: {self._output_path}")
        # Always clear the slot so a subsequent run (or the GUI's on-close
        # cleanup) can't chase a stale path.
        self._output_path = None

    def run(self) -> PipelineResult:
        """Run the three-phase pipeline. Raises ``PipelineError`` on failure.

        Phases:
          1. Download / resolve input path
          2. Silence detection
          3. Cut + concat

        The overall progress fractions are chosen per run: local files
        and cached silence detection get a smaller slice, so the bar is
        less misleading and cut+concat receives the remaining weight.

        Each phase checks ``self.cancel_event`` before starting so a
        Ctrl+C between phases aborts cleanly. Mid-phase cancellation
        comes from the cancel_callback passed to download / detect_silence
        / cut_and_concat; those raise ``*Cancelled`` exceptions that
        ``run()`` maps to ``PipelineCancelled``.
        """
        self._pipeline_start = time.monotonic()
        try:
            video_path, src_size_bytes, src_duration = self._run_download_phase()
            if self.cancel_event.is_set():
                raise PipelineCancelled("cancelled between download and silence")
            if not check_memory_reserve(
                self.cfg.memory_reserve_mb,
                "silence detection",
                self.cb.on_log,
            ):
                raise PipelineSilenceError(
                    f"Available RAM below reserve ({self.cfg.memory_reserve_mb} MB) — "
                    "cannot start silence detection. Close other applications or "
                    "reduce --memory-reserve-mb."
                )
            silence_segments = self._run_silence_phase(video_path)
            if self.cancel_event.is_set():
                raise PipelineCancelled("cancelled between silence and concat")
            if not check_memory_reserve(
                self.cfg.memory_reserve_mb,
                "concat phase",
                self.cb.on_log,
            ):
                raise PipelineConcatError(
                    f"Available RAM below reserve ({self.cfg.memory_reserve_mb} MB) — "
                    "cannot start concat phase. Close other applications or "
                    "reduce --memory-reserve-mb."
                )
            keep_segments = generate_keep_segments(video_path, silence_segments)
            keep_dur = sum(e - s for s, e in keep_segments)
            self.cb.on_info(
                build_silence_info_line(
                    num_silence=len(silence_segments),
                    num_keep=len(keep_segments),
                    keep_duration=keep_dur,
                )
            )
            output_path = self._run_concat_phase(video_path, silence_segments, keep_dur)
            return self._finish(video_path, output_path, src_size_bytes, src_duration, keep_dur)
        except PipelineCancelled as e:
            # A mid-download cancel is the only case where the file on
            # disk is known-truncated; any later phase's cancel arrives
            # after the download is complete, and unlinking it would
            # cost the user a multi-GB re-fetch. ``partial`` carries the
            # distinction through the phase wrapper.
            self._cleanup_download_path(partial_only=not e.partial)
            self._cleanup_partial_output()
            raise
        except PipelineError:
            # Already mapped by a phase method (PipelineDownloadError,
            # PipelineSilenceError, PipelineConcatError). Re-raise as-is
            # so the caller sees the specific phase that failed. These
            # always fire after ``_run_download_phase`` returned, so the
            # source (when downloaded) is complete — ``partial_only``,
            # and the latched ``_download_complete`` flag keeps it.
            self._cleanup_download_path(partial_only=True)
            self._cleanup_partial_output()
            raise
        except (CancelledError, SilenceCancelledError, DownloadCancelledError) as e:
            # DownloadCancelledError.partial=True means the cancel fired
            # while yt-dlp was still writing; anything else surfaced from
            # a later phase where the file is complete. ``not partial``
            # keeps the file on disk for retry.
            self._cleanup_download_path(
                partial_only=not (isinstance(e, DownloadCancelledError) and e.partial)
            )
            self._cleanup_partial_output()
            raise PipelineCancelled(str(e)) from e
        except DownloadError as e:
            # URLValidationError is a DownloadError subclass — no separate
            # clause needed. Only a download-time failure should purge
            # what may still be a partial byte-sink; once silence/concat
            # is underway the file is complete.
            self._cleanup_download_path(partial_only=True)
            self._cleanup_partial_output()
            raise PipelineDownloadError(str(e)) from e
        except SilenceDetectionError as e:
            self._cleanup_download_path(partial_only=True)
            self._cleanup_partial_output()
            raise PipelineSilenceError(str(e)) from e
        except ConcatError as e:
            self._cleanup_download_path(partial_only=True)
            self._cleanup_partial_output()
            raise PipelineConcatError(str(e)) from e
        except Exception as e:
            logger.exception("Pipeline unexpected error")
            self._cleanup_download_path(partial_only=True)
            self._cleanup_partial_output()
            raise PipelineUnexpectedError(str(e)) from e

    # ── Phase 1: Download / resolve ──────────────────────────────

    def _run_download_phase(self) -> tuple[Path, int, float | None]:
        """Download the URL (or passthrough local file).

        Returns ``(video_path, src_size_bytes, src_duration)`` on
        success. Raises ``PipelineDownloadError`` on download failure
        and ``PipelineCancelled`` on user cancel.
        """
        self.cb.on_progress(0.0)
        self._set_phase_progress(0.0)
        self._set_status("Step 1/4: Resolving input...", force=True)
        self.cb.on_log("Step 1/4: Downloading / resolving video...")

        try:
            download_result = download(
                self.cfg.input_raw,
                self.cfg.output_dir,
                cancel_callback=lambda: self.cancel_event.is_set(),
                quality=self.cfg.download_quality,
                progress_callback=self.cb.on_download_progress,
                download_timeout=self.cfg.download_timeout,
                connect_timeout=self.cfg.connect_timeout,
                no_progress_timeout=self.cfg.no_progress_timeout,
                proxy=self.cfg.proxy,
            )
        except DownloadCancelledError as e:
            # Mid-download cancel leaves a truncated file; surface that
            # through the pipeline-level exception so ``run()``'s
            # cleanup can decide partial-vs-complete.
            raise PipelineCancelled(str(e), partial=e.partial) from e
        except (
            VideoNotAvailableError,
            DownloadTimeoutError,
            DiskSpaceError,
            PermissionDeniedError,
            FileBusyError,
            DownloadError,
            URLValidationError,
        ) as e:
            raise PipelineDownloadError(str(e)) from e

        video_path = download_result.path
        # Per-video project directory (the function honours
        # per_video_dir itself).
        output_dir, video_path = apply_per_video_dir(
            self.cfg.output_dir,
            video_path,
            download_result.is_downloaded,
            per_video_dir=self.cfg.per_video_dir,
        )
        # Re-bind output_dir on the controller so phases 2+ use it.
        # dataclass(frozen=False) on PipelineConfig would be cleaner;
        # for now we mutate a local var and pass it to phase 2/3.
        self._output_dir_resolved = output_dir

        self._download_path = video_path if download_result.is_downloaded else None
        self._download_was_real = download_result.is_downloaded
        # From here on the download, when one happened, is *complete*:
        # the phases above (see ``_cleanup_download_path`` docstring)
        # switch to partial-only cleanup so a later cancel/error leaves
        # the file for the user's next retry.
        self._download_complete = download_result.is_downloaded

        # Fire the mid-pipeline hook so the GUI can update its
        # recent-projects panel, output label, and file-info widgets
        # BEFORE silence detection starts. The CLI doesn't use this.
        if self.on_output_resolved is not None:
            try:
                self.on_output_resolved(output_dir, video_path, download_result.is_downloaded)
            except Exception:
                logger.debug("on_output_resolved raised", exc_info=True)

        try:
            src_size_bytes = video_path.stat().st_size
        except OSError as e:
            # Between ``download()`` and this stat the file can be yanked
            # (antivirus quarantine, user cleanup). Surface a clear
            # download-phase error, not a generic "Unexpected: WinError 2".
            raise PipelineDownloadError(
                f"Downloaded file is no longer readable: {video_path} ({e})"
            ) from e
        src_duration = get_video_duration(video_path)
        self._src_duration = src_duration
        self._progress_plan = _build_progress_plan(
            is_downloaded=download_result.is_downloaded,
            src_duration=src_duration,
            silence_cache_hit=False,
        )
        dl_w, silence_w, cut_w, concat_w = self._progress_plan.weights_percent()
        concat_l = self._progress_plan.weights_percent_legacy()[2]
        self._emit_progress_plan()
        self.cb.on_log(
            f"Progress weights: download {dl_w}%, silence {silence_w}%, cutting {cut_w}%, concatenating {concat_w}% "
            f"[concat total {concat_l}%]"
        )
        self.cb.on_log(f"Size: {fmt_size(src_size_bytes)}")

        file_size_mb = math.ceil(src_size_bytes / 1024 / 1024)
        if self.cfg.method == "batch" and file_size_mb > 4096:
            self.cb.on_log(
                f"[WARN] File is {file_size_mb} MB — batch mode may use a lot of RAM. "
                "If it crashes, re-run with method=segment."
            )

        if download_result.is_downloaded:
            self._set_phase_progress(1.0)
            self._set_status("Step 1/4: Download complete", force=True)
            self.cb.on_log(f"Downloaded: {self.cfg.input_raw} -> {video_path}")
        else:
            self._set_phase_progress(1.0)
            self._set_status("Step 1/4: Local file ready", force=True)
            self.cb.on_log(f"Download skipped (file already on disk): {video_path}")

        return video_path, src_size_bytes, src_duration

    # ── Phase 2: Silence detection ───────────────────────────────

    def _run_silence_phase(self, video_path: Path) -> list[SilenceSegment]:
        """Run silence detection (with cache + resume support).

        Returns the margin-applied silence segments. Raises
        ``PipelineSilenceError`` on ffmpeg failure and
        ``PipelineCancelled`` on user cancel.
        """
        # _run_download_phase sets this before _run_silence_phase runs;
        # fall back to cfg.output_dir only for a direct unit-test call.
        output_dir = self._output_dir_resolved or self.cfg.output_dir
        self.cb.on_progress(self._progress_plan.download_end)
        self._set_phase_progress(0.0)
        self._set_status("Step 2/4: Detecting silence...", force=True)
        self.cb.on_log(
            f"Step 2/4: Detecting silence "
            f"(threshold={self.cfg.threshold}dB, "
            f"min_silence={self.cfg.min_silence}s, "
            f"margin={self.cfg.margin}s)..."
        )

        config = {
            "threshold": self.cfg.threshold,
            "min_silence": self.cfg.min_silence,
            "margin": self.cfg.margin,
        }

        # Canonical resume path shared with the CLI (fix-plan #4): both
        # front-ends must address the same checkpoint file, otherwise a
        # GUI-cancelled run is invisible to the CLI resume and vice versa.
        resume_cache_path = build_resume_cache_path(video_path, output_dir)
        if self.cfg.force and resume_cache_path.exists():
            try:
                resume_cache_path.unlink()
                self.cb.on_log("Cleared stale resume cache (force re-detect)")
            except OSError as e:
                self.cb.on_log(f"[WARN] Could not clear resume cache: {e}")
        # P2 audit: a leftover ``.inuse`` from a crashed previous run
        # takes precedence over ``.resume`` in ``detect_silence``
        # (see silence/pipeline.py). Wiping only the canonical ``.resume``
        # left the ``.inuse`` checkpoint behind, so a ``--force``
        # re-detect silently continued from the old, possibly-shifted
        # timeline instead of starting fresh. Clear both on force.
        if self.cfg.force:
            inuse_path = resume_inuse_path(resume_cache_path)
            if inuse_path.exists():
                try:
                    inuse_path.unlink()
                    self.cb.on_log("Cleared stale resume cache .inuse (force re-detect)")
                except OSError as e:
                    self.cb.on_log(f"[WARN] Could not clear .inuse cache: {e}")

        cache = None if self.cfg.force else load_silence_cache(video_path, output_dir, config)
        if cache is not None:
            self._progress_plan = _build_progress_plan(
                is_downloaded=self._download_was_real,
                src_duration=self._src_duration,
                silence_cache_hit=True,
            )
            self.cb.on_log(f"Loaded {len(cache)} silence segments from cache")
            dl_w, silence_w, cut_w, concat_w = self._progress_plan.weights_percent()
            concat_l = self._progress_plan.weights_percent_legacy()[2]
            self._emit_progress_plan()
            self.cb.on_log(
                f"Progress weights adjusted: download {dl_w}%, "
                f"silence {silence_w}%, cutting {cut_w}%, concatenating {concat_w}% "
                f"[concat total {concat_l}%]"
            )
            # Point 4: make the cache hit visible — a flash-through
            # otherwise looks like the phase didn't run.
            self._set_phase_progress(1.0)
            self._set_status("Step 2/4: Silence (cached)", force=True)
            self.cb.on_progress(self._progress_plan.silence_end)
            return cache

        silence_start = time.monotonic()
        controller = self

        def silence_prog(f: float) -> None:
            elapsed = time.monotonic() - silence_start
            controller._set_phase_progress(f)
            if f > 0.01:
                remaining = elapsed / f - elapsed
                controller.cb.on_progress(controller._progress_plan.map_silence(f))
                controller._set_status(
                    f"Step 2/4: Silence... {f * 100:.0f}% "
                    f"({fmt_time(elapsed)}/{fmt_time(remaining)})"
                )
                controller.cb.on_overall(elapsed, remaining, True)
            else:
                controller.cb.on_progress(controller._progress_plan.download_end)
                controller._set_status(
                    f"Step 2/4: Silence... {fmt_time(elapsed)} (calculating ETA)"
                )
                controller.cb.on_overall(elapsed, None, True)

        silence_segments = detect_silence(
            video_path,
            threshold=self.cfg.threshold,
            min_silence=self.cfg.min_silence,
            margin=self.cfg.margin,
            output_dir=output_dir,
            progress_callback=silence_prog,
            cancel_callback=lambda: self.cancel_event.is_set(),
            on_segment=self.on_live_segment,
            resume_cache_path=resume_cache_path,
            timeout=self.cfg.silence_timeout,
        )
        save_silence_cache(video_path, silence_segments, output_dir, config)
        try:
            resume_cache_path.unlink(missing_ok=True)
        except OSError as e:
            self.cb.on_log(f"[WARN] Could not clean up resume cache: {e}")
        self.cb.on_progress(self._progress_plan.silence_end)
        self._set_phase_progress(1.0)
        self.cb.on_log(f"Detected {len(silence_segments)} silence segments")
        return silence_segments

    # ── Phase 3: Cut + concat ────────────────────────────────────

    def _run_concat_phase(
        self,
        video_path: Path,
        silence_segments: list[SilenceSegment],
        keep_dur: float,
    ) -> Path:
        """Run cut+concat. Returns the output path on success.

        Raises ``PipelineConcatError`` on ffmpeg failure and
        ``PipelineCancelled`` on user cancel.
        """
        output_dir = self._output_dir_resolved or self.cfg.output_dir
        self.cb.on_progress(self._progress_plan.silence_end)

        # Output filename extension follows the chosen output_format.
        # ``video`` keeps the historical ``_compressed.mp4`` name; the
        # audio-only formats use the codec's native extension (mp3, opus,
        # m4a, wav, flac). Kept in sync with cli.py's suffix resolution
        # via OUTPUT_FORMAT_SPECS.
        from stream2video.config import OUTPUT_FORMAT_SPECS

        if self.cfg.output_format == "video":
            output_suffix = "compressed.mp4"
        else:
            spec = OUTPUT_FORMAT_SPECS.get(self.cfg.output_format)
            if spec is None:
                # Unreachable: validate_pipeline_config already rejected
                # an unknown output_format. Defensive guard so a future
                # caller that bypasses validation gets a clear error.
                raise PipelineConcatError(
                    f"Internal error: no spec for output_format {self.cfg.output_format!r}"
                )
            output_suffix = f"compressed.{spec['ext']}"
        output_path = output_dir / f"{video_path.stem}_{output_suffix}"
        self._output_path = output_path

        # Pre-flight disk space estimate (warning only — does not cancel,
        # matches memory_reserve warning semantics). Shared estimator also
        # used for the Start-button popup (utils.estimate_disk_need).
        try:
            from stream2video.utils import estimate_disk_need as _estimate_need

            src_size = 0
            try:
                if video_path.exists():
                    src_size = video_path.stat().st_size
            except OSError:
                pass
            required_typical, required_worst = _estimate_need(
                src_size, self._src_duration, keep_dur, self.cfg.method
            )

            # Disk check: probe the *destination* drive, walking up to
            # the nearest existing ancestor when the output dir doesn't
            # exist yet (first-run case). Previously this fell back to
            # ``video_path.parent`` — the SOURCE drive — so a source on
            # C: with output on D: would check the wrong disk entirely.
            from stream2video.utils import resolve_disk_probe

            disk_probe = resolve_disk_probe(output_dir)
            ok_typ, free = _check_disk_space(disk_probe, required_typical)
            ok_worst, _ = _check_disk_space(disk_probe, required_worst)
            if free is not None:
                if not ok_worst:
                    self.cb.on_log(
                        f"[WARN] Free space {fmt_size(free)} < worst-case peak "
                        f"{fmt_size(required_worst)} (method={self.cfg.method}, "
                        f"typical ~{fmt_size(required_typical)}). "
                        f"Need ~{fmt_size(max(0, required_worst - free))} more — may hit "
                        f"'No space left' during gapless tree L0."
                    )
                elif not ok_typ:
                    self.cb.on_log(
                        f"[WARN] Free space {fmt_size(free)} < typical peak "
                        f"{fmt_size(required_typical)} (worst ~{fmt_size(required_worst)}). "
                        f"Will likely succeed but free ~{fmt_size(max(0, required_typical - free))} more."
                    )
                else:
                    self.cb.on_log(
                        f"Disk check: free {fmt_size(free)}, need typical "
                        f"{fmt_size(required_typical)} / worst {fmt_size(required_worst)} — OK"
                    )
            else:
                self.cb.on_log(
                    f"Disk estimate: need typical {fmt_size(required_typical)} / "
                    f"worst {fmt_size(required_worst)} (free unknown)"
                )
        except Exception:
            logger.debug("disk estimate failed", exc_info=True)

        cut_start = time.monotonic()
        concat_start: float | None = None
        current_phase = "cutting"
        controller = self

        def _set_phase(name: str, *, force: bool = False) -> None:
            nonlocal current_phase, concat_start
            if name == current_phase:
                return
            current_phase = name
            if name == "concatenating":
                concat_start = time.monotonic()
                controller._set_phase_progress(0.0)
                controller._set_status("Step 4/4: Concatenating...", force=True)
                controller.cb.on_log("Step 4/4: Concatenating segments...")
                controller.cb.on_progress(controller._progress_plan.cut_end)

        def _on_phase(name: str, f: float) -> None:
            # Atomic dispatch: cutting 0..1 → silence_end..cut_end,
            # concatenating 0..1 → cut_end..concat_end. Each phase gets
            # its own thin bar + ETA so a stall in gapless tree L0 is
            # distinguishable from segment encodes.
            if name == "cutting":
                if current_phase != "cutting":
                    _set_phase("cutting")
                elapsed = time.monotonic() - cut_start
                controller._set_phase_progress(f)
                if f > 0.01:
                    remaining = elapsed / f - elapsed
                    controller.cb.on_progress(controller._progress_plan.map_cut(f))
                    controller._set_status(
                        f"Step 3/4: Cutting... {f * 100:.0f}% "
                        f"({fmt_time(elapsed)}/{fmt_time(remaining)})"
                    )
                    controller.cb.on_overall(elapsed, remaining, False)
                else:
                    controller.cb.on_progress(controller._progress_plan.silence_end)
                    controller._set_status(
                        f"Step 3/4: Cutting... {fmt_time(elapsed)} (calculating ETA)"
                    )
                    controller.cb.on_overall(elapsed, None, False)
            else:
                if current_phase != "concatenating":
                    _set_phase("concatenating", force=True)
                if concat_start is None:
                    # Invariant guard, not control flow: `_set_phase` above
                    # sets concat_start; raising beats a TypeError from
                    # ``time.monotonic() - None`` if the order regresses
                    # (and unlike ``assert`` this survives ``python -O``).
                    raise RuntimeError("concat phase entered without concat_start")
                elapsed = time.monotonic() - concat_start
                controller._set_phase_progress(f)
                if f > 0.01:
                    remaining = elapsed / f - elapsed
                    controller.cb.on_progress(controller._progress_plan.map_concat(f))
                    controller._set_status(
                        f"Step 4/4: Concatenating... {f * 100:.0f}% "
                        f"({fmt_time(elapsed)}/{fmt_time(remaining)})"
                    )
                    controller.cb.on_overall(elapsed, remaining, False)
                else:
                    controller.cb.on_progress(controller._progress_plan.cut_end)
                    controller._set_status(
                        f"Step 4/4: Concatenating... {fmt_time(elapsed)} (calculating ETA)"
                    )
                    controller.cb.on_overall(elapsed, None, False)

        # Legacy fallback for callers without on_phase (tests mocking
        # cut_and_concat with progress_callback). Keep the old single-phase
        # shape but map it onto 3 so back-compat tests see 3.
        def concat_prog(f: float) -> None:
            _on_phase(
                "cutting" if f < 0.9 else "concatenating", f / 0.9 if f < 0.9 else (f - 0.9) / 0.1
            )

        # Announce atomic split in logs so the user's report shows
        # [16:13:43] Step 3/4 Cutting + [16:14:xx] Step 4/4 Concatenating
        # instead of the monolithic "[16:14:14] [ERROR] gapless tree L0 G0".
        self.cb.on_progress(self._progress_plan.silence_end)
        self._set_phase_progress(0.0)
        self._set_status("Step 3/4: Cutting...", force=True)
        self.cb.on_log(
            f"Step 3/4: Cutting "
            f"(method={self.cfg.method}, encoder={self.cfg.encoder}, "
            f"video_quality={self.cfg.video_quality}, "
            f"output_format={self.cfg.output_format})..."
        )

        cut_and_concat(
            video_path,
            silence_segments,
            output_path,
            progress_callback=concat_prog,
            on_phase=_on_phase,
            method=self.cfg.method,
            encoder=self.cfg.encoder,
            video_quality=self.cfg.video_quality,
            audio_quality=self.cfg.audio_quality,
            cancel_callback=lambda: self.cancel_event.is_set(),
            software_fallback=self.cfg.software_fallback,
            x264_preset=self.cfg.x264_preset,
            encoder_threads=self.cfg.encoder_threads,
            output_fps=self.cfg.output_fps,
            output_format=self.cfg.output_format,
            memory_limit_mb=self.cfg.memory_limit_mb,
            memory_reserve_mb=self.cfg.memory_reserve_mb,
            x264_low_memory=self.cfg.x264_low_memory,
            use_crf=self.cfg.use_crf,
            gapless_concat=self.cfg.gapless_concat,
            low_process_priority=self.cfg.low_process_priority,
            rlimit_as_mb=self.cfg.rlimit_as_mb,
            segment_encode_timeout=self.cfg.segment_encode_timeout,
            final_concat_timeout=self.cfg.final_concat_timeout,
            stall_kill_timeout=self.cfg.stall_kill_timeout,
            stall_warning_timeout=self.cfg.stall_warning_timeout,
            batch_chunk_size=self.cfg.batch_chunk_size,
            min_part_bytes=self.cfg.min_part_bytes,
            fallback_consent=self.on_fallback_consent,
        )

        self._output_path = None
        self._set_phase_progress(1.0)
        return output_path

    # ── Phase 4: Finish ──────────────────────────────────────────

    def _finish(
        self,
        video_path: Path,
        output_path: Path,
        src_size_bytes: int,
        src_duration: float | None,
        keep_dur: float,
    ) -> PipelineResult:
        """Build the success summary and clean up."""
        self.cb.on_progress(self._progress_plan.concat_end)
        try:
            dst_size_bytes = output_path.stat().st_size
        except OSError:
            # The output was there a moment ago (cut_and_concat returned
            # successfully on it) — an antivirus or the user removed it
            # in the gap between encode-finish and this stat. Report 0
            # rather than crashing the whole pipeline at the 100% mark;
            # the summary's other fields still describe a real success.
            dst_size_bytes = 0
        total_elapsed = time.monotonic() - self._pipeline_start

        summary = {
            "src_size_bytes": src_size_bytes,
            "src_duration": src_duration,
            "dst_size_bytes": dst_size_bytes,
            "keep_duration": keep_dur,
            "pipeline_seconds": total_elapsed,
            "output_path": str(output_path),
            "video_path": str(video_path),
        }
        self.cb.on_pipeline_complete(summary)

        # Delete downloaded source if requested.
        if self.cfg.delete_after and self._download_path is not None:
            try:
                self._download_path.unlink()
                self.cb.on_log(f"Deleted source: {self._download_path}")
            except OSError as e:
                self.cb.on_log(f"[WARN] Could not delete source: {e}")
        self._download_path = None

        return PipelineResult(
            output_path=output_path,
            video_path=video_path,
            src_size_bytes=src_size_bytes,
            src_duration=src_duration,
            dst_size_bytes=dst_size_bytes,
            keep_duration=keep_dur,
            pipeline_seconds=total_elapsed,
        )


def validate_pipeline_config(cfg: PipelineConfig) -> list[str]:
    """Pure validation: return a list of human-readable error strings.

    Empty list = config is valid. Pure: no I/O, no side effects, so it
    can be unit-tested without instantiating the GUI or running ffmpeg.
    Catches the common mistakes (negative thresholds, empty input,
    unknown method) before the worker thread starts so the user sees
    a clear error instead of a mid-pipeline crash.
    """
    errors: list[str] = []
    if not cfg.input_raw.strip():
        errors.append("Input is empty — provide a URL or local file path.")
    if cfg.method not in ("segment", "batch", "cut_then_encode"):
        errors.append(
            f"Unknown method {cfg.method!r} (use 'segment', 'batch', or 'cut_then_encode')."
        )
    if cfg.encoder not in ("h264_nvenc", "h264_amf", "h264_mf", "libx264"):
        errors.append(f"Unknown encoder {cfg.encoder!r}.")
    if cfg.video_quality not in VALID_QUALITIES:
        errors.append(f"Unknown video_quality {cfg.video_quality!r}.")
    if cfg.audio_quality not in VALID_QUALITIES:
        errors.append(f"Unknown audio_quality {cfg.audio_quality!r}.")
    if cfg.output_format not in ("video", "mp3", "opus", "aac", "wav", "flac"):
        errors.append(
            f"Unknown output_format {cfg.output_format!r} "
            "(use 'video', 'mp3', 'opus', 'aac', 'wav', or 'flac')."
        )
    if not -60 <= cfg.threshold <= -5:
        errors.append(f"threshold {cfg.threshold} out of range [-60, -5].")
    if not 0.1 <= cfg.min_silence <= 60:
        errors.append(f"min_silence {cfg.min_silence} out of range [0.1, 60].")
    if not -3 <= cfg.margin <= 5:
        errors.append(f"margin {cfg.margin} out of range [-3, 5].")
    return errors
