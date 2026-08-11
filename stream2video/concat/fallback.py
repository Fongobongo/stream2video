"""libx264 fallback policy and method dispatcher.

``_with_libx264_fallback`` is the single place that decides whether a
failing primary encoder should be silently retried with libx264;
``_run_with_fallback`` uses it to dispatch into one of the three
video pipelines (segment / batch / cut_then_encode).
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import (
    _BATCH_CHUNK_SIZE,
    _FINAL_CONCAT_TIMEOUT,
    _MIN_PART_BYTES,
    _SEGMENT_ENCODE_TIMEOUT,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.config import VALID_METHODS
from stream2video.memory import MemoryMonitor

logger = logging.getLogger(__name__)


def _run_with_fallback(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    primary_codec: str,
    primary_opts: list[str],
    method: str,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    video_quality: str = "medium",
    audio_quality: str = "medium",
    software_fallback: str = "ask",
    fallback_consent: Callable[[], bool] | None = None,
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    source_has_audio: bool = True,
    output_fps: str = "source",
    x264_low_memory: bool = False,
    use_crf: bool = False,
    source_bitrate: int | None = None,
    gapless_concat: bool = False,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    batch_chunk_size: int = _BATCH_CHUNK_SIZE,
    min_part_bytes: int = _MIN_PART_BYTES,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Run the picked concat method with the primary encoder; fall back to libx264 on failure.

    On encoder fallback the per-method working directory (``_<stem>_segments``
    / ``_<stem>_batch`` / ``_<stem>_cut``) is wiped -- the chunks written by
    the failing encoder may be corrupt (e.g. h264_mf produces MP4s without a
    moov atom on some Windows builds) and the resume-skip check in the inner
    method would otherwise reuse them on the libx264 retry.

    ``method`` is one of ``VALID_METHODS`` ("segment", "batch",
    "cut_then_encode"); anything else raises ConcatError.
    ``video_quality`` / ``audio_quality`` are forwarded to the libx264
    fallback so the retry uses the same bitrate/CRF/AAC preset the user
    requested. ``software_fallback`` / ``fallback_consent`` gate the
    retry per the policy in :func:`_with_libx264_fallback`.
    ``x264_preset`` / ``encoder_threads`` likewise forward so the fallback
    respects a low-CPU intent.
    """
    if method == "segment":
        work_suffix = "_segments"
    elif method == "batch":
        work_suffix = "_batch"
    elif method == "cut_then_encode":
        work_suffix = "_cut"
    else:
        raise _c.ConcatError(
            f"Unknown method: {method!r} (use {' or '.join(repr(m) for m in VALID_METHODS)})"
        )

    work_dir = output_path.parent / f"_{output_path.stem}{work_suffix}"

    def _cleanup(failed_enc: str) -> None:
        if work_dir.exists():
            logger.info(
                f"Removing partial {work_suffix[1:]} dir from failed {failed_enc} encode: {work_dir}"
            )
            shutil.rmtree(work_dir, ignore_errors=True)

    def _try(enc: str, enc_opts: list[str]) -> None:
        if method == "segment":
            _c._run_segment_concat(
                video_path,
                keep_segments,
                output_path,
                enc,
                enc_opts,
                progress_callback,
                cancel_callback,
                encoder=enc,
                video_quality=video_quality,
                audio_quality=audio_quality,
                x264_preset=x264_preset,
                encoder_threads=encoder_threads,
                source_has_audio=source_has_audio,
                output_fps=output_fps,
                gapless_concat=gapless_concat,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                segment_encode_timeout=segment_encode_timeout,
                final_concat_timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                min_part_bytes=min_part_bytes,
                memory_monitor_factory=memory_monitor_factory,
            )
        elif method == "cut_then_encode":
            _c._run_cut_then_encode(
                video_path,
                keep_segments,
                output_path,
                enc,
                enc_opts,
                progress_callback,
                cancel_callback,
                encoder=enc,
                video_quality=video_quality,
                audio_quality=audio_quality,
                x264_preset=x264_preset,
                encoder_threads=encoder_threads,
                source_has_audio=source_has_audio,
                output_fps=output_fps,
                segment_encode_timeout=segment_encode_timeout,
                final_concat_timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                min_part_bytes=min_part_bytes,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
                x264_low_memory=x264_low_memory,
            )
        else:
            _c._run_batch_concat(
                video_path,
                keep_segments,
                output_path,
                enc,
                enc_opts,
                progress_callback,
                cancel_callback,
                encoder=enc,
                video_quality=video_quality,
                audio_quality=audio_quality,
                x264_preset=x264_preset,
                encoder_threads=encoder_threads,
                source_has_audio=source_has_audio,
                output_fps=output_fps,
                segment_encode_timeout=segment_encode_timeout,
                final_concat_timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                batch_chunk_size=batch_chunk_size,
                min_part_bytes=min_part_bytes,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )

    _c._with_libx264_fallback(
        primary_codec,
        primary_opts,
        _try,
        (_c.ConcatError, OSError),
        _cleanup,
        video_quality=video_quality,
        audio_quality=audio_quality,
        software_fallback=software_fallback,
        fallback_consent=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        x264_low_memory=x264_low_memory,
        use_crf=use_crf,
        source_bitrate=source_bitrate,
    )


def _with_libx264_fallback(
    primary_codec: str,
    primary_opts: list[str],
    try_fn: Callable[[str, list[str]], None],
    exc_types: tuple[type[BaseException], ...],
    on_fallback: Callable[[str], None] | None = None,
    video_quality: str = "medium",
    audio_quality: str = "medium",
    software_fallback: str = "ask",
    fallback_consent: Callable[[], bool] | None = None,
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
    use_crf: bool = False,
    source_bitrate: int | None = None,
) -> None:
    """Run ``try_fn(primary_codec, primary_opts)``; on failure, retry once with libx264.

    Behaviour on ``primary_codec`` failure depends on ``software_fallback``:

      * ``enabled`` -- retry with libx264 (legacy silent-fallback behaviour).
      * ``disabled`` -- re-raise the original exception immediately so the
        user gets the real encoder's error.
      * ``ask`` (default) -- call ``fallback_consent``; if it returns True
        retry with libx264, otherwise re-raise. When ``fallback_consent``
        is None the policy re-raises so an unattended run cannot silently
        switch to a CPU-heavy encoder.

    ``on_fallback`` (optional): called with the failing encoder name
    BEFORE retrying with libx264. Use this to clean up partial / corrupt
    output (e.g. delete a segment directory of MP4s that have a missing
    moov atom).

    ``video_quality`` and ``audio_quality`` are forwarded to the libx264
    retry so its CRF/AAC bitrate matches the user-requested preset.
    ``x264_preset`` / ``encoder_threads`` likewise follow the user's
    settings so the fallback respects the low-CPU intent for users who
    chose ``ultrafast`` + a thread cap to protect an unstable CPU.
    ``x264_low_memory`` reduces the encoder's frame-buffer footprint
    (see ``encoder_opts`` for details).

    Two exception classes bypass the retry entirely (fix-plan #10):

      * ``FFmpegOutOfMemoryError`` — the hardware encoder died by OOM.
        Retrying with libx264 (which allocates MORE memory for its
        frame buffers) would just OOM again; better to surface the
        "lower the memory budget" hint to the user immediately.
      * ``EncoderUnavailableError`` — the driver/encoder was never even
        tried (a config problem, not a video-data problem), so a libx264
        retry adds no information.
    """
    enc, enc_opts = primary_codec, primary_opts
    while True:
        try:
            try_fn(enc, enc_opts)
            return
        except _c.CancelledError:
            raise
        except (_c.FFmpegOutOfMemoryError, _c.EncoderUnavailableError):
            # Never retry these with libx264 — see docstring.
            raise
        except exc_types as e:
            if enc == "libx264":
                raise
            # Non-libx264 encoder failed -- apply fallback policy.
            if software_fallback == "disabled":
                raise
            if software_fallback == "ask" and (fallback_consent is None or not fallback_consent()):
                raise
            # software_fallback == "enabled" OR ask-consented.
            logger.warning(f"{enc} failed: {str(e)[:200]}; falling back to libx264")
            if on_fallback is not None:
                try:
                    on_fallback(enc)
                except Exception as cleanup_err:
                    logger.warning(f"Cleanup before libx264 retry failed: {cleanup_err}")
            enc, enc_opts = (
                "libx264",
                _c.encoder_opts(
                    "libx264",
                    video_quality,
                    x264_preset=x264_preset,
                    encoder_threads=encoder_threads,
                    x264_low_memory=x264_low_memory,
                    use_crf=use_crf,
                    source_bitrate=source_bitrate,
                ),
            )
