"""Pipeline controller extracted from ``gui.py`` (Этап 10 incremental).

The pipeline worker in ``gui._pipeline_worker`` is a ~300-line method
that interleaves:
  * pure pipeline orchestration (download → silence → cut+concat)
  * Tk widget updates (progress bar, status label, log textbox)
  * cancel / error handling
  * per-step progress mapping (download 0..5%, silence 5..60%, cut 60..100%)

This module defines the dataclass and callback interface that let the
orchestration be unit-tested without a Tk main loop. The actual run
logic still lives in ``gui._pipeline_worker`` for now — this is the
skeleton that a future refactor will populate. New code should use
``PipelineConfig`` and ``PipelineCallbacks`` so the migration stays
incremental.

Why not extract the whole run() in one go:
  * The run() body is deeply intertwined with ``self._ui_progress(0.0)``
    and ``self._ui_status(...)`` calls that are scheduled on the Tk
    main loop via ``self._tk_after``. Moving them requires designing
    a callback protocol that preserves the main-thread dispatch
    guarantee (Tk widgets are not thread-safe).
  * The download progress callback (``_download_cb``) is a closure
    over ``download_start`` and ``self._ui_*`` — extracting it needs
    a state object to carry the timing anchor.
  * Error handling distinguishes CancelledError / ConcatError /
    DownloadError subclasses and maps each to a specific Tk dialog —
    that mapping belongs in the GUI, not in a pure controller.

The skeleton here lets us at least validate the config shape and
the callback signatures in tests, and gives the next refactor a
target to fill in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stream2video.download import DownloadProgress


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
    force: bool
    delete_after: bool
    per_video_dir: bool
    threshold: float
    min_silence: float
    margin: float
    memory_limit_mb: str | int
    memory_reserve_mb: int
    download_timeout: int
    connect_timeout: int
    no_progress_timeout: int


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
    if cfg.method not in ("segment", "batch"):
        errors.append(f"Unknown method {cfg.method!r} (use 'segment' or 'batch').")
    if cfg.encoder not in ("h264_nvenc", "h264_amf", "h264_mf", "libx264"):
        errors.append(f"Unknown encoder {cfg.encoder!r}.")
    if cfg.video_quality not in ("high", "medium", "low"):
        errors.append(f"Unknown video_quality {cfg.video_quality!r}.")
    if cfg.audio_quality not in ("high", "medium", "low"):
        errors.append(f"Unknown audio_quality {cfg.audio_quality!r}.")
    if not -60 <= cfg.threshold <= -5:
        errors.append(f"threshold {cfg.threshold} out of range [-60, -5].")
    if not 0.1 <= cfg.min_silence <= 60:
        errors.append(f"min_silence {cfg.min_silence} out of range [0.1, 60].")
    if not -3 <= cfg.margin <= 5:
        errors.append(f"margin {cfg.margin} out of range [-3, 5].")
    return errors
