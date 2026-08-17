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
from stream2video.concat.options import ConcatOptions
from stream2video.concat.output_lock import (
    LockHandle,
    acquire_output_lock,
    release_output_lock,
)
from stream2video.config import VALID_OUTPUT_FORMATS

if TYPE_CHECKING:
    from stream2video.silence import SilenceSegment

logger = logging.getLogger(__name__)


def _make_phase_progress(
    progress_callback: Callable[[float], None] | None,
    on_phase: Callable[[str, float], None] | None,
) -> Callable[[float], None] | None:
    """Build the single progress funnel every inner method reports through.

    When the caller provided ``on_phase``, split 0..0.9 → cutting and
    0.9..1.0 → concatenating and dispatch to ``on_phase`` instead of the
    legacy 0..1 ``progress_callback`` (tests/CLI without ``on_phase``
    keep the legacy path). Then apply the high-water-mark clamp:
    ffmpeg's ``out_time_us`` is not strictly monotonic — near the tail of
    an encode the muxer finalises packets and the progress stream can
    emit a value slightly LOWER than the previous one (observed on the
    ubuntu runner: 0.8976 → 0.9 → 0.8955). Both the audio-only extract
    and every video method funnel their reports through the returned
    callback, so clamping here once is the single place that guarantees
    the user-facing bar never runs backwards — per-segment callbacks,
    chunk callbacks and the final concat all benefit without each
    reinventing its own latch.

    Call EXACTLY ONCE per run, before the audio/video fork: the
    audio-only path and the video path must report through the same
    wrapper instance, or the monotonic latch and the 90/10 phase mapping
    reset mid-run. The previous code built the wrapper twice and the
    video path threw the first instance away (audit round 14 P3).
    """
    inner_progress = progress_callback
    if on_phase is not None:

        def _wrap(f: float) -> None:
            f = max(0.0, min(1.0, f))
            if f < 0.9:
                on_phase("cutting", f / 0.9)
            elif f < 1.0:
                on_phase("concatenating", (f - 0.9) / 0.1)
            else:
                on_phase("concatenating", 1.0)

        inner_progress = _wrap

    if inner_progress is not None:
        _hwm = {"v": 0.0}
        _base = inner_progress

        def _mono(f: float) -> None:
            if f < _hwm["v"]:
                return
            _hwm["v"] = f
            _base(f)

        inner_progress = _mono

    return inner_progress


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
    # CRF mode: quality-fixed encoding instead of bitrate-fixed (-b:v).
    # libx264 uses CRF, NVENC/AMF use CQ/QP-style modes, and MF uses
    # quality mode. Default False keeps bitrate parity across encoders.
    use_crf: bool = False,
    gapless_concat: bool = False,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill_timeout: int = _STALL_KILL,
    stall_warning_timeout: int = _STALL_WARNING,
    batch_chunk_size: int = _BATCH_CHUNK_SIZE,
    min_part_bytes: int = _MIN_PART_BYTES,
    # Pre-acquired project lock (the pipeline controller's project
    # lock covers this output — audit round 24 P4): when provided, the
    # output lock is NOT re-acquired, because the project lock already
    # serialises every run that could write this output (and acquiring
    # a second lock inside the project lock would risk a lock-order
    # inversion with a direct API caller that holds only the output
    # lock). A caller WITHOUT a project lock keeps the historical
    # self-acquiring behaviour.
    lock: LockHandle | None = None,
) -> Path:
    if not video_path.exists():
        raise _c.ConcatError(f"Input video not found: {video_path}")

    if output_format not in VALID_OUTPUT_FORMATS:
        raise _c.ConcatError(
            f"Unknown output_format {output_format!r} "
            f"(use {' or '.join(repr(f) for f in VALID_OUTPUT_FORMATS)})"
        )

    # An exclusive lock file prevents a second concurrent
    # run (GUI + CLI, two CLIs) from interleaving -y writes into the
    # same output_path and silently corrupting each other. It MUST be
    # taken before any probe/encoder work below (ffprobe, generate_keep,
    # encoder smoke-test) — otherwise a losing second run still spawns
    # subprocesses and burns GPU/CPU seconds before acquiring the lock
    # and failing, which violates the "fail fast" design intent and
    # makes GUI+CLI collisions look "stuck" during the other run's pre-
    # ambles. All body code below is inside the try/finally that drops
    # the lock on every exit path.
    def _locked_body() -> Path:
        return _run_locked(
            video_path=video_path,
            silence_segments=silence_segments,
            output_path=output_path,
            progress_callback=progress_callback,
            on_phase=on_phase,
            method=method,
            encoder=encoder,
            video_quality=video_quality,
            audio_quality=audio_quality,
            cancel_callback=cancel_callback,
            software_fallback=software_fallback,
            x264_preset=x264_preset,
            encoder_threads=encoder_threads,
            fallback_consent=fallback_consent,
            output_fps=output_fps,
            output_format=output_format,
            x264_low_memory=x264_low_memory,
            use_crf=use_crf,
            gapless_concat=gapless_concat,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
            segment_encode_timeout=segment_encode_timeout,
            final_concat_timeout=final_concat_timeout,
            stall_kill_timeout=stall_kill_timeout,
            stall_warning_timeout=stall_warning_timeout,
            batch_chunk_size=batch_chunk_size,
            min_part_bytes=min_part_bytes,
            memory_limit_mb=memory_limit_mb,
            memory_reserve_mb=memory_reserve_mb,
        )

    if lock is not None:
        return _locked_body()
    _lock_path = acquire_output_lock(output_path)
    try:
        return _locked_body()
    finally:
        release_output_lock(_lock_path)


def _run_locked(
    video_path: Path,
    silence_segments: "list[SilenceSegment]",
    output_path: Path,
    progress_callback: Callable[[float], None] | None,
    on_phase: Callable[[str, float], None] | None,
    method: str,
    encoder: str,
    video_quality: str,
    audio_quality: str,
    cancel_callback: Callable[[], bool] | None,
    software_fallback: str,
    x264_preset: str,
    encoder_threads: str | int,
    fallback_consent: Callable[[], bool] | None,
    output_fps: str,
    output_format: str,
    x264_low_memory: bool,
    use_crf: bool,
    gapless_concat: bool,
    low_process_priority: bool,
    rlimit_as_mb: int,
    segment_encode_timeout: int,
    final_concat_timeout: int,
    stall_kill_timeout: int,
    stall_warning_timeout: int,
    batch_chunk_size: int,
    min_part_bytes: int,
    memory_limit_mb: str | int,
    memory_reserve_mb: int,
) -> Path:
    """Body of :func:`cut_and_concat` that runs under the output lock.

    Split out of the public entry so the lock-acquire lands before ANY
    subprocess work (ffprobe, encoder smoke-test); see the comment at
    the ``acquire_output_lock`` call site.
    """
    keep_segments = _c.generate_keep_segments(video_path, silence_segments)
    memory_monitor_factory = _c._make_memory_monitor_factory(memory_limit_mb, memory_reserve_mb)
    options = ConcatOptions(
        encoder=encoder,
        video_quality=video_quality,
        audio_quality=audio_quality,
        software_fallback=software_fallback,
        fallback_consent=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        output_fps=output_fps,
        x264_low_memory=x264_low_memory,
        use_crf=use_crf,
        gapless_concat=gapless_concat,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
        segment_encode_timeout=segment_encode_timeout,
        final_concat_timeout=final_concat_timeout,
        stall_kill=stall_kill_timeout,
        stall_warning=stall_warning_timeout,
        batch_chunk_size=batch_chunk_size,
        min_part_bytes=min_part_bytes,
        memory_limit_mb=memory_limit_mb,
        memory_reserve_mb=memory_reserve_mb,
        memory_monitor_factory=memory_monitor_factory,
    )

    if not keep_segments:
        raise _c.ConcatError("No video segments to keep after removing silence")

    logger.info(
        f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments"
    )

    # One shared progress funnel for BOTH the audio-only and the video
    # paths, built before the fork (see _make_phase_progress): the
    # monotonic latch and the 90/10 phase mapping must survive across it.
    inner_progress = _make_phase_progress(progress_callback, on_phase)

    # Audio-only output path: short-circuit the video pipeline entirely.
    # The segment/batch/cut_then_encode paths are video-oriented (they
    # spend GPU/CPU on H.264 encoding); for an audio-only output the
    # video stream is dropped and the per-segment encode is a cheap
    # audio re-encode. The user's ``encoder`` / ``video_quality`` /
    # ``output_fps`` / ``x264_*`` choices are irrelevant here, so the
    # video encoder isn't even probed. See OUTPUT_FORMAT_SPECS in
    # config.py for the codec/container mapping.
    # The output lock is held by the caller — no second acquire here.
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
            progress_callback=inner_progress,
            cancel_callback=cancel_callback,
            options=options,
        )
        return output_path

    # Honest source bitrate probe when quality==source in bitrate mode.
    # ffprobe's per-stream ``bit_rate`` is ``N/A`` for most muxes that
    # don't store it explicitly — jumping straight to the fixed "high"
    # 10 Mbps preset there would inflate a 3 Mbps source ~3x. Estimate
    # the overall bitrate from file size / duration first (the same
    # heuristic ``estimate_disk_need`` uses — includes container and
    # audio overhead, which is fine for a "don't regress vs source"
    # target); only fall back to the "high" preset when even the
    # container duration is unprobed.
    source_bitrate: int | None = None
    effective_opts_quality = video_quality
    if video_quality == "source" and not use_crf:
        from stream2video.utils import (
            get_video_bitrate as _gvb,
        )
        from stream2video.utils import (
            get_video_duration as _gvd,
        )

        source_bitrate = _gvb(video_path)
        if source_bitrate is None or source_bitrate <= 0:
            estimate: int | None = None
            try:
                est_duration = _gvd(video_path)
                est_size = video_path.stat().st_size
                if est_duration and est_duration > 0 and est_size > 0:
                    estimate = int(est_size * 8 / est_duration)
            except OSError as e:
                logger.debug(f"source bitrate estimate failed for {video_path.name}: {e}")
            if estimate and estimate > 0:
                source_bitrate = estimate
                logger.info(
                    f"ffprobe stream bit_rate=N/A for {video_path.name} — "
                    f"estimated source bitrate {estimate / 1000:.0f}k from "
                    f"file size/duration"
                )
            else:
                logger.warning(
                    f"source bitrate probe failed for {video_path.name} "
                    f"(ffprobe returned N/A and duration unknown) — falling "
                    f"back to high quality ({_c._VIDEO_BITRATES['high']}) "
                    f"for {encoder}. File will be larger than source; "
                    f"consider probing manually or using high/medium."
                )
                effective_opts_quality = "high"
        else:
            logger.info(
                f"Probed source video bitrate: {source_bitrate / 1000:.0f}k for {encoder} source"
            )
    vcodec, vcodec_opts = _c.get_video_encoder(
        encoder,
        effective_opts_quality,
        software_fallback=software_fallback,
        on_unavailable=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        x264_low_memory=x264_low_memory,
        source_bitrate=source_bitrate,
        use_crf=use_crf,
    )
    # If probe failed we already fell back via effective_opts_quality; for
    # the HW source case where probe succeeded, get_video_encoder used
    # source_bitrate to emit -b:v. Log final choice.
    if video_quality == "source" and not use_crf and source_bitrate is None:
        logger.info(
            f"Encoder: {vcodec} {vcodec_opts} (quality=source→{effective_opts_quality} fallback)"
        )
    else:
        logger.info(f"Encoder: {vcodec} {vcodec_opts} (quality={video_quality})")

    # Detect whether the source has an audio stream ONCE. Probing per
    # segment would be wasteful; passing the flag down lets the
    # segment/batch builders omit ``-c:a`` / audio mapping for
    # audio-less sources (otherwise ffmpeg fails with "Output file
    # does not contain any stream" when ``-map 0:a:0`` is requested
    # on a video-only input).
    source_has_audio = _c.has_audio_stream(video_path)
    if not source_has_audio:
        logger.info(f"Source {video_path.name} has no audio stream -- encoding video-only")
    options = options.replace(
        source_bitrate=source_bitrate,
        source_has_audio=source_has_audio,
    )

    # The output lock is held by the caller (cut_and_concat); this helper
    # runs entirely under it, so by this point the loser run already
    # raised ConcatLockError before any subprocess was spawned.
    _c._run_with_fallback(
        video_path,
        keep_segments,
        output_path,
        vcodec,
        vcodec_opts,
        method,
        inner_progress,
        cancel_callback,
        options=options,
    )

    return output_path
