"""Pipeline controller extracted from ``gui.py`` (Этап 10 incremental).

The pipeline worker in ``gui._pipeline_worker`` is a ~350-line method
that interleaves:
  * pure pipeline orchestration (download → silence → cut+concat)
  * Tk widget updates (progress bar, status label, log textbox)
  * cancel / error handling
  * per-step progress mapping (download 0..5%, silence 5..40%, cut 40..100%)

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
    PermissionDeniedError,
    URLValidationError,
    VideoNotAvailableError,
    download,
)
from stream2video.formatters import fmt_size, fmt_time
from stream2video.gui_helpers import build_silence_info_line
from stream2video.memory import _available_ram_mb
from stream2video.paths import apply_per_video_dir
from stream2video.silence import (
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceSegment,
    detect_silence,
    load_silence_cache,
    save_silence_cache,
)
from stream2video.utils import get_video_duration

logger = logging.getLogger(__name__)

_MEMORY_POLL_INTERVAL = 2.0


def _check_memory_reserve(
    cfg_memory_reserve_mb: int,
    phase_name: str,
    on_log: Callable[[str], None],
) -> bool:
    """Check if available RAM is above the reserve. Return True if OK.

    Logs a warning when the reserve is tight and returns False when
    the reserve is already violated so the caller can refuse to start
    the next heavy stage.
    """
    avail = _available_ram_mb()
    if avail is None:
        return True
    if avail < cfg_memory_reserve_mb * 1.5:
        on_log(
            f"[WARN] Available RAM {avail:.0f} MB is tight "
            f"(reserve={cfg_memory_reserve_mb} MB) — {phase_name} may be risky."
        )
    if avail < cfg_memory_reserve_mb:
        on_log(
            f"[ERROR] Available RAM {avail:.0f} MB is below reserve "
            f"{cfg_memory_reserve_mb} MB — refusing to start {phase_name}."
        )
        return False
    return True


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
    on_status: Callable[[str], None]
    on_log: Callable[[str], None]
    on_info: Callable[[str], None]
    on_overall: Callable[[float, float | None, bool], None]
    on_total: Callable[[float], None]
    on_download_progress: Callable[[DownloadProgress], None]
    on_pipeline_complete: Callable[[dict], None]


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
    """User cancelled the pipeline (cancel_event set)."""


class PipelineDownloadError(PipelineError):
    """Download phase failed (network / disk / permission / unavailable)."""


class PipelineSilenceError(PipelineError):
    """Silence detection phase failed."""


class PipelineConcatError(PipelineError):
    """Cut+concat phase failed."""


class PipelineUnexpectedError(PipelineError):
    """Any other exception not mapped to a specific phase."""


# Progress-bar fractions for each phase. Sum to 1.0:
#   0.00..0.05  download (5%)
#   0.05..0.40  silence detection (35%)
#   0.40..1.00  cut+concat (60%)
# Kept as module constants so tests can pin them and the controller
# stays in sync with the GUI's _ui_overall more_phases flag.
PROG_DOWNLOAD_END = 0.05
PROG_SILENCE_END = 0.40
PROG_CONCAT_END = 1.00


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
    # mid-pipeline side effects that need to happen BEFORE silence
    # detection starts. Optional because the CLI doesn't need it.
    on_output_resolved: Callable[[Path, Path, bool], None] | None = None

    # Mutable per-run state. The GUI reads these after ``run()`` to
    # drive the completion summary + recent-projects panel; on error
    # they're still populated up to the point where the failure occurred.
    _download_path: Path | None = field(default=None, init=False)
    _output_path: Path | None = field(default=None, init=False)
    _pipeline_start: float = field(default=0.0, init=False)

    def run(self) -> PipelineResult:
        """Run the three-phase pipeline. Raises ``PipelineError`` on failure.

        Phases:
          1. Download / resolve input path (5% of progress bar)
          2. Silence detection (35% of progress bar)
          3. Cut + concat (60% of progress bar)

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
            if not _check_memory_reserve(
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
            if not _check_memory_reserve(
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
        except PipelineError:
            # Already mapped by a phase method (PipelineDownloadError,
            # PipelineSilenceError, PipelineConcatError, PipelineCancelled).
            # Re-raise as-is so the caller sees the specific phase that
            # failed, not a generic PipelineUnexpectedError.
            raise
        except (CancelledError, SilenceCancelledError, DownloadCancelledError) as e:
            raise PipelineCancelled(str(e)) from e
        except (DownloadError, URLValidationError) as e:
            raise PipelineDownloadError(str(e)) from e
        except SilenceDetectionError as e:
            raise PipelineSilenceError(str(e)) from e
        except ConcatError as e:
            raise PipelineConcatError(str(e)) from e
        except Exception as e:
            logger.exception("Pipeline unexpected error")
            raise PipelineUnexpectedError(str(e)) from e

    # ── Phase 1: Download / resolve ──────────────────────────────

    def _run_download_phase(self) -> tuple[Path, int, float | None]:
        """Download the URL (or passthrough local file).

        Returns ``(video_path, src_size_bytes, src_duration)`` on
        success. Raises ``PipelineDownloadError`` on download failure
        and ``PipelineCancelled`` on user cancel.
        """
        self.cb.on_progress(0.0)
        self.cb.on_status("Step 1/3: Downloading / resolving video...")
        self.cb.on_log("Step 1/3: Downloading / resolving video...")

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
            )
        except DownloadCancelledError as e:
            raise PipelineCancelled(str(e)) from e
        except (
            VideoNotAvailableError,
            DownloadTimeoutError,
            DiskSpaceError,
            PermissionDeniedError,
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

        # Fire the mid-pipeline hook so the GUI can update its
        # recent-projects panel, output label, and file-info widgets
        # BEFORE silence detection starts. The CLI doesn't use this.
        if self.on_output_resolved is not None:
            try:
                self.on_output_resolved(output_dir, video_path, download_result.is_downloaded)
            except Exception:
                logger.debug("on_output_resolved raised", exc_info=True)

        src_size_bytes = video_path.stat().st_size
        src_duration = get_video_duration(video_path)
        self.cb.on_log(f"Size: {fmt_size(src_size_bytes)}")

        file_size_mb = src_size_bytes // 1024 // 1024
        if self.cfg.method == "batch" and file_size_mb > 4096:
            self.cb.on_log(
                f"[WARN] File is {file_size_mb} MB — batch mode may use a lot of RAM. "
                "If it crashes, re-run with method=segment."
            )

        if download_result.is_downloaded:
            self.cb.on_log(f"Downloaded: {self.cfg.input_raw} -> {video_path}")
        else:
            self.cb.on_log(f"Download skipped (file already on disk): {video_path}")

        return video_path, src_size_bytes, src_duration

    # ── Phase 2: Silence detection ───────────────────────────────

    def _run_silence_phase(self, video_path: Path) -> list[SilenceSegment]:
        """Run silence detection (with cache + resume support).

        Returns the margin-applied silence segments. Raises
        ``PipelineSilenceError`` on ffmpeg failure and
        ``PipelineCancelled`` on user cancel.
        """
        output_dir = getattr(self, "_output_dir_resolved", self.cfg.output_dir)
        self.cb.on_progress(PROG_DOWNLOAD_END)
        self.cb.on_status("Step 2/3: Detecting silence...")
        self.cb.on_log(
            f"Step 2/3: Detecting silence "
            f"(threshold={self.cfg.threshold}dB, "
            f"min_silence={self.cfg.min_silence}s, "
            f"margin={self.cfg.margin}s)..."
        )

        config = {
            "threshold": self.cfg.threshold,
            "min_silence": self.cfg.min_silence,
            "margin": self.cfg.margin,
        }

        resume_cache_path = output_dir / f"{video_path.stem}_silence_cache.json.resume"
        if self.cfg.force and resume_cache_path.exists():
            try:
                resume_cache_path.unlink()
                self.cb.on_log("Cleared stale resume cache (force re-detect)")
            except OSError as e:
                self.cb.on_log(f"[WARN] Could not clear resume cache: {e}")

        cache = None if self.cfg.force else load_silence_cache(video_path, output_dir, config)
        if cache is not None:
            self.cb.on_log(f"Loaded {len(cache)} silence segments from cache")
            return cache

        silence_start = time.monotonic()
        controller = self

        def silence_prog(f: float) -> None:
            elapsed = time.monotonic() - silence_start
            if f > 0.01:
                remaining = elapsed / f - elapsed
                controller.cb.on_progress(
                    PROG_DOWNLOAD_END + f * (PROG_SILENCE_END - PROG_DOWNLOAD_END)
                )
                controller.cb.on_status(
                    f"Step 2/3: Silence... {f * 100:.0f}% "
                    f"({fmt_time(elapsed)}/{fmt_time(remaining)})"
                )
                controller.cb.on_overall(elapsed, remaining, True)
            else:
                controller.cb.on_progress(PROG_DOWNLOAD_END)
                controller.cb.on_status(
                    f"Step 2/3: Silence... {fmt_time(elapsed)} (calculating ETA)"
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
        output_dir = getattr(self, "_output_dir_resolved", self.cfg.output_dir)
        self.cb.on_progress(PROG_SILENCE_END)
        self.cb.on_status("Step 3/3: Cutting and concatenating...")
        self.cb.on_log(
            f"Step 3/3: Cutting & concatenating "
            f"(method={self.cfg.method}, encoder={self.cfg.encoder}, "
            f"video_quality={self.cfg.video_quality}, "
            f"output_format={self.cfg.output_format})..."
        )

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

        cut_start = time.monotonic()
        controller = self

        def concat_prog(f: float) -> None:
            elapsed = time.monotonic() - cut_start
            if f > 0.01:
                remaining = elapsed / f - elapsed
                controller.cb.on_progress(
                    PROG_SILENCE_END + f * (PROG_CONCAT_END - PROG_SILENCE_END)
                )
                controller.cb.on_status(
                    f"Step 3/3: Cutting... {f * 100:.0f}% "
                    f"({fmt_time(elapsed)}/{fmt_time(remaining)})"
                )
                controller.cb.on_overall(elapsed, remaining, False)
            else:
                controller.cb.on_progress(PROG_SILENCE_END)
                controller.cb.on_status(
                    f"Step 3/3: Cutting... {fmt_time(elapsed)} (calculating ETA)"
                )
                controller.cb.on_overall(elapsed, None, False)

        cut_and_concat(
            video_path,
            silence_segments,
            output_path,
            progress_callback=concat_prog,
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
            gapless_concat=self.cfg.gapless_concat,
            low_process_priority=self.cfg.low_process_priority,
            rlimit_as_mb=self.cfg.rlimit_as_mb,
            segment_encode_timeout=self.cfg.segment_encode_timeout,
            final_concat_timeout=self.cfg.final_concat_timeout,
            stall_kill_timeout=self.cfg.stall_kill_timeout,
            stall_warning_timeout=self.cfg.stall_warning_timeout,
            batch_chunk_size=self.cfg.batch_chunk_size,
            min_part_bytes=self.cfg.min_part_bytes,
        )

        self._output_path = None
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
        self.cb.on_progress(PROG_CONCAT_END)
        dst_size_bytes = output_path.stat().st_size
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
