"""``cut_and_concat`` — top-level entry point dispatched by CLI / GUI."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from stream2video import concat as _c
from stream2video.concat.constants import (
    _BATCH_CHUNK_SIZE,
    _FINAL_CONCAT_TIMEOUT,
    _MIN_PART_BYTES,
    _SEGMENT_ENCODE_TIMEOUT,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.config import VALID_OUTPUT_FORMATS

if TYPE_CHECKING:
    from stream2video.silence import SilenceSegment

logger = logging.getLogger(__name__)


def cut_and_concat(
    video_path: Path,
    silence_segments: "list[SilenceSegment]",
    output_path: Path,
    progress_callback: Callable[[float], None] | None = None,
    # Atomic phase callback: receives "cutting" (0.0..1.0) or "concatenating"
    # (0.0..1.0). When provided, the outer progress_callback is NOT used
    # (the caller maps the two phases to distinct spans). This mirrors the
    # 0.9/0.1 split inside segment/batch/cut_encode but surfaces it atomically
    # so UI can show Step 3/4 + 4/4 instead of a monolithic 3/3.
    on_phase: Callable[[str, float], None] | None = None,
    method: str = "batch",
    encoder: str = "libx264",
    video_quality: str = "medium",
    audio_quality: str = "medium",
    cancel_callback: Callable[[], bool] | None = None,
    software_fallback: str = "ask",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    fallback_consent: Callable[[], bool] | None = None,
    output_fps: str = "source",
    output_format: str = "video",
    memory_limit_mb: str | int = "auto",
    memory_reserve_mb: int = 2048,
    x264_low_memory: bool = False,
    gapless_concat: bool = False,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill_timeout: int = _STALL_KILL,
    stall_warning_timeout: int = _STALL_WARNING,
    batch_chunk_size: int = _BATCH_CHUNK_SIZE,
    min_part_bytes: int = _MIN_PART_BYTES,
) -> Path:
    if not video_path.exists():
        raise _c.ConcatError(f"Input video not found: {video_path}")

    if output_format not in VALID_OUTPUT_FORMATS:
        raise _c.ConcatError(
            f"Unknown output_format {output_format!r} "
            f"(use {' or '.join(repr(f) for f in VALID_OUTPUT_FORMATS)})"
        )

    keep_segments = _c.generate_keep_segments(video_path, silence_segments)
    memory_monitor_factory = _c._make_memory_monitor_factory(memory_limit_mb, memory_reserve_mb)

    if not keep_segments:
        raise _c.ConcatError("No video segments to keep after removing silence")

    logger.info(
        f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments"
    )

    # Audio-only output path: short-circuit the video pipeline entirely.
    # The segment/batch/cut_then_encode paths are video-oriented (they
    # spend GPU/CPU on H.264 encoding); for an audio-only output the
    # video stream is dropped and the per-segment encode is a cheap
    # audio re-encode. The user's ``encoder`` / ``video_quality`` /
    # ``output_fps`` / ``x264_*`` choices are irrelevant here, so the
    # video encoder isn't even probed. See OUTPUT_FORMAT_SPECS in
    # config.py for the codec/container mapping.
    if output_format != "video":
        source_has_audio = _c.has_audio_stream(video_path)
        if not source_has_audio:
            raise _c.ConcatError(
                f"Source {video_path.name} has no audio stream -- cannot "
                f"produce {output_format} output"
            )
        _c._run_audio_extract(
            video_path,
            keep_segments,
            output_path,
            output_format,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            audio_quality=audio_quality,
            segment_encode_timeout=segment_encode_timeout,
            final_concat_timeout=final_concat_timeout,
            stall_kill=stall_kill_timeout,
            stall_warning=stall_warning_timeout,
            min_part_bytes=min_part_bytes,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
            memory_monitor_factory=memory_monitor_factory,
        )
        return output_path

    vcodec, vcodec_opts = _c.get_video_encoder(
        encoder,
        video_quality,
        software_fallback=software_fallback,
        on_unavailable=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        x264_low_memory=x264_low_memory,
    )
    logger.info(f"Encoder: {vcodec} {vcodec_opts} (quality={video_quality})")

    # Detect whether the source has an audio stream ONCE. Probing per
    # segment would be wasteful; passing the flag down lets the
    # segment/batch builders omit ``-c:a`` / audio mapping for
    # audio-less sources (otherwise ffmpeg fails with "Output file
    # does not contain any stream" when ``-map 0:a:0`` is requested
    # on a video-only input). See P1.14 in the fix plan.
    source_has_audio = _c.has_audio_stream(video_path)
    if not source_has_audio:
        logger.info(f"Source {video_path.name} has no audio stream -- encoding video-only")

    # Wrap progress so inner methods can report atomically via on_phase.
    # When the caller provided on_phase, split 0..0.9 → cutting and 0.9..1.0 →
    # concatenating and dispatch to on_phase instead of the legacy 0..1
    # progress_callback. Keep legacy path for tests/CLI without on_phase.
    inner_progress = progress_callback
    if on_phase is not None:

        def _wrap(f: float) -> None:
            f = max(0.0, min(1.0, f))
            if f < 0.9:
                on_phase("cutting", f / 0.9 if 0.9 > 0 else 1.0)
            elif f < 1.0:
                on_phase("concatenating", (f - 0.9) / 0.1)
            else:
                on_phase("concatenating", 1.0)

        inner_progress = _wrap

    _c._run_with_fallback(
        video_path,
        keep_segments,
        output_path,
        vcodec,
        vcodec_opts,
        method,
        inner_progress,
        cancel_callback,
        video_quality=video_quality,
        audio_quality=audio_quality,
        software_fallback=software_fallback,
        fallback_consent=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        source_has_audio=source_has_audio,
        output_fps=output_fps,
        x264_low_memory=x264_low_memory,
        gapless_concat=gapless_concat,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
        segment_encode_timeout=segment_encode_timeout,
        final_concat_timeout=final_concat_timeout,
        stall_kill=stall_kill_timeout,
        stall_warning=stall_warning_timeout,
        batch_chunk_size=batch_chunk_size,
        min_part_bytes=min_part_bytes,
        memory_monitor_factory=memory_monitor_factory,
    )

    return output_path
