"""Video cutting and concatenation module using ffmpeg."""

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stream2video.config import (
    OUTPUT_FORMAT_SPECS,
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FORMATS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
)
from stream2video.memory import MemoryMonitor, auto_budget_mb
from stream2video.silence import SilenceSegment
from stream2video.tools import ffmpeg_path, ffprobe_path, popen_with_retry, run_with_retry
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    get_video_duration,
    get_video_start_time,
    has_audio_stream,
    looks_like_oom,
    no_window_kwargs,
    read_lines_queue,
    registered_process,
    subprocess_kwargs,
)

logger = logging.getLogger(__name__)


class ConcatError(Exception):
    """Raised on concat / encode failures (ffmpeg errors, bad inputs)."""


class FFmpegError(ConcatError):
    """ffmpeg itself failed (non-zero exit, timeout, stall)."""


class FFmpegOutOfMemoryError(FFmpegError):
    """ffmpeg was killed by the OS OOM killer or self-aborted on alloc.

    Distinct from ``FFmpegError`` so the CLI / GUI can surface a
    targeted "lower the memory budget / use Low-memory preset" hint
    instead of dumping a generic ffmpeg stderr snippet.

    Detection (in ``_run_ffmpeg``):

    * POSIX: ``returncode == -9`` (Python convention for "child killed
      by signal SIGKILL") or ``returncode == 137`` (128 + 9, the shell
      convention) — the Linux OOM killer sends SIGKILL.
    * stderr markers (case-insensitive, cross-platform): "out of
      memory", "cannot allocate memory", "malloc failed", "mmap
      failed", "not enough space", "Error splitting input into
      thread: Cannot allocate memory" (libx264's thread init failure).
      On Windows exit code is 1 (generic) so stderr is the only
      signal.
    """


class CancelledError(ConcatError):
    """User cancellation during concat/encode (not a real failure)."""


class EncoderUnavailableError(ConcatError):
    """Hardware encoder unavailable and the fallback policy refused libx264.

    Distinct from ``FFmpegError`` so the CLI can craft a "select a different
    encoder / check the driver" message instead of a generic "ffmpeg failed"
    one -- the encoder wasn't even tried, so its stderr wouldn't be helpful.
    """


def _quote_concat_path(p: str) -> str:
    """Quote a path for ffmpeg's concat demuxer file list.

    ffmpeg's concat demuxer skips backslash sequences when finding the closing
    quote but stores them LITERALLY in the filename (verified with ffmpeg 8.1.1).
    We therefore avoid backslash escapes entirely. We pick the quote character
    not present in the path; if both are present we raise, since ffmpeg cannot
    safely represent such a path.
    """
    if "'" not in p and '"' not in p and not any(c.isspace() for c in p):
        return p
    if "'" not in p:
        return f"'{p}'"
    if '"' not in p:
        return f'"{p}"'
    raise ConcatError(
        f"Path contains both quote types, cannot be represented: {p}. "
        f"Rename the file or move it into a directory whose path doesn't contain quotes."
    )


_VIDEO_BITRATE = "7000k"
# Audio bitrate presets. ``medium`` is the default 192k preset; ``high``/``low``
# give the user a real choice so a 192k/256k/320k source is no longer silently
# downgraded to 128k. ``source`` skips the bitrate/resample/downmix policy.
# P1 audit v0.3 §6.1: callers pass ``audio_quality`` explicitly via
# ``_audio_bitrate_opts(q)`` / ``_audio_opts(q)``, eliminating the module-level
# ``_audio_quality`` global that made consecutive runs share mutable state.
_AUDIO_BITRATE = "128k"
_AUDIO_BITRATES: dict[str, str] = {
    "high": "256k",
    "medium": "192k",
    "low": "128k",
}
# Sample rate / channel policy. ``-ar 48000 -ac 2`` historically
# normalised everything to stereo 48 kHz AAC -- the source was never
# preserved, but output was at least consistent across segments. Keep
# that explicit conversion so the audio path is documented, but route
# it through ``_audio_opts(q)`` so an explicit "preserve source" preset
# can be added as a follow-up without rewriting every call site.
_AUDIO_SAMPLE_RATE = "48000"
_AUDIO_CHANNELS = "2"
_BATCH_CHUNK_SIZE = 40
# Minimum chunk size used for small files that would produce too many
# tiny chunks; also protects against zero-length chunk lists.
_BATCH_CHUNK_MIN = 5
ENCODER_CHECK_TIMEOUT = 10
_FINAL_CONCAT_TIMEOUT = 86400
_SEGMENT_ENCODE_TIMEOUT = 600
_STDERR_TRUNCATE = 1000
_STALL_WARNING = 120
_STALL_KILL = 300
# Minimum size (bytes) for a resumed part to be considered valid.
# Exposed via CONFIG_DEFAULTS (``min_part_bytes``).
_MIN_PART_BYTES = 1024


def _audio_bitrate(audio_quality: str = "") -> str:
    """Bitrate string for the AAC encoder based on ``audio_quality``.

    Empty string (the default) falls back to ``_AUDIO_BITRATE`` (128k)
    only so trivial test/benchmark call sites that don't go through
    ``cut_and_concat`` keep their historical output. Real pipeline paths
    always pass an explicit quality (``source``/``high``/``medium``/``low``)
    through their ``audio_quality`` parameter; unknown values raise
    :class:`ConcatError` so a typo doesn't silently fall back to 128k.
    """
    if audio_quality == "":
        return _AUDIO_BITRATE
    if audio_quality == "source":
        return ""
    if audio_quality not in _AUDIO_BITRATES:
        raise ConcatError(
            f"Unknown audio quality {audio_quality!r} "
            f"(use {' or '.join(repr(k) for k in VALID_QUALITIES)})"
        )
    return _AUDIO_BITRATES[audio_quality]


def _audio_bitrate_opts(audio_quality: str = "") -> list[str]:
    """Return ``-b:a`` opts for lossy audio, or none for ``source``."""
    bitrate = _audio_bitrate(audio_quality)
    return ["-b:a", bitrate] if bitrate else []


def _audio_opts(audio_quality: str = "") -> list[str]:
    """Output-side AAC options: sample rate + channel layout.

    ``source`` returns no ``-ar`` / ``-ac`` flags, allowing ffmpeg to keep
    the decoded stream's native sample rate and channel layout where the
    selected output codec supports it. Other presets keep the historical
    stereo 48 kHz normalisation. Returns a fresh list each call so callers
    may mutate freely.
    """
    if audio_quality == "source":
        return []
    if audio_quality not in ("", *_AUDIO_BITRATES):
        raise ConcatError(
            f"Unknown audio quality {audio_quality!r} "
            f"(use {' or '.join(repr(k) for k in _AUDIO_BITRATES)})"
        )
    return ["-ar", _AUDIO_SAMPLE_RATE, "-ac", _AUDIO_CHANNELS]


# Bitrate (HW encoders) and CRF (libx264) per ``video_quality`` preset.
# ``medium`` keeps the values previously hard-coded in ENCODER_OPTS so
# existing output size/quality is unchanged on upgrade.
_VIDEO_BITRATES: dict[str, str] = {
    "high": "10000k",
    "medium": _VIDEO_BITRATE,
    "low": "3500k",
}
_X264_CRF: dict[str, str] = {
    "high": "18",
    "medium": "23",
    "low": "28",
}

_encoder_check_cache: dict[str, bool] = {}
_encoder_check_lock = threading.Lock()


def _memory_budget_mb(memory_limit_mb: str | int) -> float | None:
    """Resolve the user-facing memory limit value to a numeric MB budget."""
    if memory_limit_mb == "auto":
        return auto_budget_mb()
    if memory_limit_mb is None:
        return None
    try:
        value = float(memory_limit_mb)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid memory_limit_mb=%r", memory_limit_mb)
        return None
    return value if value > 0 else None


def _make_memory_monitor_factory(
    memory_limit_mb: str | int,
    memory_reserve_mb: int,
) -> Callable[[str], MemoryMonitor | None] | None:
    budget_mb = _memory_budget_mb(memory_limit_mb)
    reserve_mb = max(0.0, float(memory_reserve_mb))
    if budget_mb is None and reserve_mb <= 0:
        return None
    if budget_mb is not None:
        logger.info(
            "Memory guardrail: RSS budget %.0fMB, reserve %.0fMB (warning-only)",
            budget_mb,
            reserve_mb,
        )
    else:
        logger.info(
            "Memory guardrail: RSS budget disabled, reserve %.0fMB (warning-only)",
            reserve_mb,
        )

    def _factory(label: str) -> MemoryMonitor | None:
        return MemoryMonitor(
            0,
            memory_limit_mb=budget_mb,
            memory_reserve_mb=reserve_mb,
            label=label,
        )

    return _factory


def _new_memory_monitor(
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None,
    label: str,
) -> MemoryMonitor | None:
    return memory_monitor_factory(label) if memory_monitor_factory is not None else None


def cut_and_concat(
    video_path: Path,
    silence_segments: list[SilenceSegment],
    output_path: Path,
    progress_callback: Callable[[float], None] | None = None,
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
        raise ConcatError(f"Input video not found: {video_path}")

    if output_format not in VALID_OUTPUT_FORMATS:
        raise ConcatError(
            f"Unknown output_format {output_format!r} "
            f"(use {' or '.join(repr(f) for f in VALID_OUTPUT_FORMATS)})"
        )

    keep_segments = generate_keep_segments(video_path, silence_segments)
    memory_monitor_factory = _make_memory_monitor_factory(memory_limit_mb, memory_reserve_mb)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

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
        source_has_audio = has_audio_stream(video_path)
        if not source_has_audio:
            raise ConcatError(
                f"Source {video_path.name} has no audio stream -- cannot "
                f"produce {output_format} output"
            )
        _run_audio_extract(
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

    vcodec, vcodec_opts = get_video_encoder(
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
    source_has_audio = has_audio_stream(video_path)
    if not source_has_audio:
        logger.info(f"Source {video_path.name} has no audio stream -- encoding video-only")

    _run_with_fallback(
        video_path,
        keep_segments,
        output_path,
        vcodec,
        vcodec_opts,
        method,
        progress_callback,
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


def generate_keep_segments(
    video_path: Path,
    silence_segments: list[SilenceSegment],
) -> list[tuple[float, float]]:
    duration = get_video_duration(video_path)
    if duration is None:
        raise ConcatError("Could not determine video duration via ffprobe")

    if duration <= 0:
        raise ConcatError(f"Invalid video duration: {duration}")

    valid = []
    for s in silence_segments:
        start = max(0.0, float(s.start))
        end = min(float(duration), float(s.end))
        if end <= start:
            continue
        # Only warn on a meaningful clamp -- sub-microsecond FP drift
        # between source timestamps and the probed duration would
        # otherwise fire a noisy warning on every segment of the second
        # pass.
        if abs(s.start - start) > 1e-6 or abs(s.end - end) > 1e-6:
            logger.warning(
                f"Silence segment ({s.start:.2f}s - {s.end:.2f}s) "
                f"clamped to ({start:.2f}s - {end:.2f}s) to fit duration {duration:.2f}s"
            )
        valid.append((start, end))

    sorted_silences = sorted(valid, key=lambda s: s[0])
    keep_segments = []
    current_time = 0.0

    for start, end in sorted_silences:
        if current_time < start:
            keep_segments.append((current_time, start))
        current_time = max(current_time, end)

    if current_time < duration:
        keep_segments.append((current_time, duration))

    return keep_segments


def _x264_low_memory_opts() -> list[str]:
    """Return extra x264 options that reduce peak memory usage.

    These tune the lookahead, reference frames, and B-frame pyramid so
    the encoder holds fewer frame buffers in RAM simultaneously. The
    trade-off is slightly worse compression efficiency (larger file for
    the same CRF), which is acceptable when the alternative is an OOM
    kill on a memory-constrained machine.
    """
    return [
        "-x264-params",
        "rc-lookahead=10:ref=1:bframes=0",
    ]


def encoder_opts(
    encoder: str,
    quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
) -> list[str]:
    """Return the ffmpeg encoder options for ``encoder`` at ``quality`` preset.

    quality: ``source`` / ``high`` / ``medium`` / ``low``. ``high``/
    ``medium``/``low`` affect bitrate (HW encoders) and CRF (libx264).
    ``source`` omits the project bitrate/CRF policy and lets ffmpeg's
    encoder defaults apply. ``medium`` reproduces the previously hard-coded
    options exactly so existing output is unchanged.

    ``x264_preset`` (libx264 only): one of ``VALID_X264_PRESETS``. Default
    ``medium`` preserves historical behaviour; users with unstable /
    overclocked CPUs can pass ``ultrafast``/``veryfast`` for a lighter
    load. See P0.5 in the fix plan.

    ``encoder_threads``: ``"auto"`` (no ``-threads`` flag, ffmpeg chooses)
    or a positive int. For libx264 the flag goes AFTER the encoder in the
    constructed command (``-c:v libx264 ... -threads N``) so it applies to
    the encoder, not to the decoder (a ``-threads`` before ``-i`` would
    bound the decoder's thread pool instead -- different effect).

    ``x264_low_memory`` (libx264 only): when True, appends
    ``-x264-params rc-lookahead=10:ref=1:bframes=0`` to reduce the
    encoder's frame-buffer footprint. Useful on memory-constrained
    machines (4-8 GB RAM) where a default-medium encode of a long
    stream could push the process into swap.
    """
    if encoder not in VALID_ENCODERS:
        raise ConcatError(f"Unknown encoder {encoder!r} (known: {', '.join(VALID_ENCODERS)})")
    if quality not in VALID_QUALITIES:
        raise ConcatError(
            f"Unknown video quality {quality!r} (use {' or '.join(repr(q) for q in VALID_QUALITIES)})"
        )
    if x264_preset not in VALID_X264_PRESETS:
        raise ConcatError(
            f"Unknown x264 preset {x264_preset!r} "
            f"(use {' or '.join(repr(p) for p in VALID_X264_PRESETS)})"
        )
    threads_opt = _threads_opt(encoder_threads)

    if quality == "source":
        low_mem = _x264_low_memory_opts() if encoder == "libx264" and x264_low_memory else []
        if encoder == "libx264":
            return ["-preset", x264_preset, *threads_opt, *low_mem]
        return [*threads_opt]

    bitrate = _VIDEO_BITRATES[quality]
    if encoder == "h264_mf":
        # Media Foundation: no preset/threads control via -preset; pass
        # -threads only when the user pinned a count (auto = omit).
        return ["-b:v", bitrate, "-quality", "100", *threads_opt]
    if encoder == "h264_amf":
        return ["-usage", "transcoding", "-quality", "speed", "-b:v", bitrate, *threads_opt]
    if encoder == "h264_nvenc":
        # NVENC rate-control model (P2.12): constrained VBR via
        # ``-rc vbr`` with ``-b:v`` (target) and ``-maxrate`` (cap)
        # both set to the preset bitrate, plus ``-cq 18`` as the
        # quality floor. This is NVIDIA's recommended RC model for
        # offline encoding: VBR lets the encoder spend bits where
        # they're needed (motion, detail) while ``-maxrate``
        # guarantees a worst-case size, and ``-cq`` prevents quality
        # from dropping below 18 even when the bitrate budget would
        # allow it. ``-preset p7`` is the slowest / highest-quality
        # NVENC preset (lookahead enabled, 2-pass). On a 6h stream
        # this is ~5-10x faster than libx264 -preset medium at
        # similar quality.
        return [
            "-preset",
            "p7",
            "-rc",
            "vbr",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-cq",
            "18",
            *threads_opt,
        ]
    # libx264 -- CRF-driven, bitrate is ignored. ``-preset`` controls the
    # speed/size trade-off AND the CPU load (slower presets do more
    # lookahead/motion search). ``-threads`` caps the encoder's parallel
    # frame evaluations so an 8-core machine doesn't saturate to 100% on
    # a long encode.
    crf = _X264_CRF[quality]
    low_mem = _x264_low_memory_opts() if x264_low_memory else []
    return ["-crf", crf, "-preset", x264_preset, *threads_opt, *low_mem]


def _threads_opt(encoder_threads: str | int) -> list[str]:
    """Return the ffmpeg ``-threads`` arg list for the encoder position.

    ``"auto"`` → no flag (ffmpeg picks, same as before).
    Positive int → ``["-threads", "N"]``. Anything else (zero, negative,
    non-int string) is dropped with a warning so the encoder doesn't get
    a malformed flag that ffmpeg would reject.
    """
    if encoder_threads == "auto":
        return []
    if isinstance(encoder_threads, bool):
        return []
    if isinstance(encoder_threads, int) and encoder_threads > 0:
        return ["-threads", str(encoder_threads)]
    logger.warning(f"encoder_threads={encoder_threads!r} is not 'auto' or a positive int; ignoring")
    return []


def _fps_filter_chain(output_fps: str) -> str:
    """Return the ffmpeg filter chain fragment that enforces ``output_fps``.

    ``source`` (default) returns an empty string -- the encoder receives
    the original PTS without any fps filter, so a 30 FPS source comes
    out at 30 FPS without frame duplication. This is the historical
    behaviour preserved by the trim+concat rewrite.

    A numeric value (``24`` / ``25`` / ``30`` / ``50`` / ``60``) returns
    a ``fps=<target>`` filter fragment. Callers must splice it into the
    filter chain AFTER ``setpts=PTS-STARTPTS`` so the new PTS cadence
    is the source's, not the synthetic ``N/FRAME_RATE`` one.

    The ``fps`` filter duplicates or drops frames to match the target
    CFR; the docs warn that this changes file size and quality.
    """
    if output_fps == "source":
        return ""
    if output_fps in VALID_OUTPUT_FPS:
        return f",fps={output_fps}"
    logger.warning(
        f"output_fps={output_fps!r} is not 'source' or one of {VALID_OUTPUT_FPS}; ignoring"
    )
    return ""


# Public back-compat registry (P2.11): maps each supported encoder to
# its default (medium) options. Kept as a documented public API because:
#   1. Tests use it as a sanity check that VALID_ENCODERS and the
#      encoder registry stay in sync.
#   2. Downstream tools that import stream2video as a library may rely
#      on it to enumerate available encoders and their default options.
# Runtime callers should prefer ``encoder_opts(encoder, quality)`` so
# the ``video_quality`` / ``x264_preset`` / ``encoder_threads`` presets
# are applied; ENCODER_OPTS always returns the ``medium`` defaults.
ENCODER_OPTS: dict[str, list[str]] = {enc: encoder_opts(enc) for enc in VALID_ENCODERS}


def check_encoder(name: str) -> bool:
    """Smoke test: verify the encoder works by encoding 1 frame. Cached per process.

    Previously ``libx264`` returned ``True`` without actually invoking
    ffmpeg -- that hid cases where the build's libx264 was broken or
    missing (e.g. a stripped-down distro build, or ffmpeg linked with
    GPL-removed codecs). We now run the same 1-frame lavfi smoke test
    for every encoder; the cache keeps it free for the test-encoder
    button's repeated calls.
    """
    with _encoder_check_lock:
        if name in _encoder_check_cache:
            return _encoder_check_cache[name]
        try:
            r = run_with_retry(
                [
                    ffmpeg_path(),
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=0.1",
                    "-c:v",
                    name,
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=ENCODER_CHECK_TIMEOUT,
                **no_window_kwargs(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} smoke test timed out after {ENCODER_CHECK_TIMEOUT}s")
            _encoder_check_cache[name] = False
            return False
        ok = r.returncode == 0
        if not ok and name == "libx264":
            # libx264 is the safety net -- if it's gone, the user needs to
            # reinstall ffmpeg with x264 support, not be told to "pick
            # a different encoder".
            logger.error(
                f"libx264 smoke test FAILED (rc={r.returncode}); "
                f"stderr: {r.stderr[:300]!r}. Your ffmpeg build is missing the "
                f"x264 library -- reinstall ffmpeg (e.g. `winget install "
                f"Gyan.FFmpeg` on Windows) or install a build with libx264 support."
            )
        _encoder_check_cache[name] = ok
        return ok


def get_video_encoder(
    preferred: str,
    video_quality: str = "medium",
    software_fallback: str = "ask",
    on_unavailable: Callable[[], bool] | None = None,
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    x264_low_memory: bool = False,
) -> tuple[str, list[str]]:
    """Resolve the encoder to use for this run.

    If ``preferred`` is available, return it. If not (smoke test fails),
    consult ``software_fallback``:

      * ``enabled`` -- return libx264 (legacy silent-fallback behaviour).
      * ``disabled`` -- raise EncoderUnavailableError immediately.
      * ``ask`` (default) -- call ``on_unavailable`` if provided; the
        callback returns True to allow libx264 fallback, False to
        raise. With ``on_unavailable=None``, ``ask`` behaves as
        ``disabled`` so a pipeline run can't silently switch to a
        CPU-heavy encoder without consent.

    ``x264_preset`` and ``encoder_threads`` are forwarded to
    :func:`encoder_opts` so the resolved options match the user's
    settings (also on a fallback retry -- the fallback call site keeps
    the same values). ``x264_low_memory`` reduces x264 frame buffer
    usage (see ``encoder_opts`` for details).
    """
    if preferred not in VALID_ENCODERS:
        raise ConcatError(f"Unknown encoder {preferred!r} (known: {', '.join(VALID_ENCODERS)})")
    if video_quality not in VALID_QUALITIES:
        raise ConcatError(
            f"Unknown video quality {video_quality!r} "
            f"(use {' or '.join(repr(q) for q in VALID_QUALITIES)})"
        )
    if software_fallback not in VALID_SOFTWARE_FALLBACKS:
        raise ConcatError(
            f"Unknown software_fallback {software_fallback!r} "
            f"(use {' or '.join(repr(s) for s in VALID_SOFTWARE_FALLBACKS)})"
        )

    if check_encoder(preferred):
        return preferred, encoder_opts(
            preferred,
            video_quality,
            x264_preset=x264_preset,
            encoder_threads=encoder_threads,
            x264_low_memory=x264_low_memory,
        )

    # HW encoder unavailable -- apply fallback policy.
    if software_fallback == "enabled":
        logger.warning(f"{preferred} not available, falling back to libx264")
        return "libx264", encoder_opts(
            "libx264",
            video_quality,
            x264_preset=x264_preset,
            encoder_threads=encoder_threads,
            x264_low_memory=x264_low_memory,
        )
    if software_fallback == "ask":
        if on_unavailable is None:
            raise EncoderUnavailableError(
                f"{preferred} not available; install the encoder or "
                f"select a different one (software_fallback='ask' but "
                f"no consent handler was provided)"
            )
        if on_unavailable():
            logger.warning(f"{preferred} not available; user consented to libx264 fallback")
            return "libx264", encoder_opts(
                "libx264",
                video_quality,
                x264_preset=x264_preset,
                encoder_threads=encoder_threads,
                x264_low_memory=x264_low_memory,
            )
        raise EncoderUnavailableError(f"{preferred} not available; user declined libx264 fallback")
    # software_fallback == "disabled"
    raise EncoderUnavailableError(
        f"{preferred} not available; software_fallback='disabled' -- refusing libx264"
    )


def _run_ffmpeg(
    cmd: list[str],
    progress_callback: Callable[[float], None] | None,
    timeout: int,
    label: str = "ffmpeg",
    cancel_callback: Callable[[], bool] | None = None,
    track_progress: bool = True,
    memory_monitor: "MemoryMonitor | None" = None,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> None:
    """Run an ffmpeg command. With track_progress=True (default), parses ffmpeg's
    -progress stream from stdout and invokes progress_callback(seconds). With False,
    stdout is discarded -- use for per-segment encodes where the segment index
    already implies progress.

    Polls cancel_callback every CANCEL_POLL_INTERVAL seconds during the final
    wait so long-running encodes can be aborted promptly. Stall detection
    (no progress for _STALL_KILL seconds -> kill) only runs in the
    track_progress=True branch: per-segment encodes get their progress
    implicitly from the segment index, so per-byte stalls aren't meaningful.

    ``memory_monitor`` (optional, P1.17): when provided, the monitor's
    daemon thread is started AFTER the subprocess is spawned and stopped
    in the finally block. The monitor fires ``cancel_callback`` on a
    hard memory threshold, which routes through the same cancel path
    the user's Ctrl+C uses. None disables the monitor (preserves
    historical behaviour for callers that haven't been updated).
    """
    stdout_target = subprocess.PIPE if track_progress else subprocess.DEVNULL
    # Debug logging to help diagnose spawn failures from real GUI runs. When
    # this exception fires, we want to know exactly what was attempted.
    logger.debug(
        f"spawning ffmpeg: cmd[0]={cmd[0]!r}, "
        f"cmdlen={len(cmd)}, path_exists={Path(cmd[0]).is_file()}, "
        f"cwd={os.getcwd()!r}, shell={os.getenv('COMSPEC', '?')}"
    )
    try:
        process = popen_with_retry(
            cmd,
            # stdin=DEVNULL is CRITICAL on Windows when the parent is a
            # pythonw.exe (GUI subsystem) launched from cmd.exe with an
            # attached console: inheriting the parent's console-mode stdin
            # handle is the documented trigger for CreateProcessW to fail
            # with ERROR_FILENAME_EXCED_RANGE (winerror 206) — the exact
            # error observed in production runs on 2026-08-02/03. See
            # CPython issue 37380 and the note in stream2video.tools.
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        logger.error(
            "ffmpeg spawn failed: cmd[0]=%r exists=%s cmdlen=%d winerror=%s "
            "filename=%r strerror=%r cwd=%r env_path_prefix=%r",
            cmd[0],
            Path(cmd[0]).is_file(),
            len(cmd),
            getattr(e, "winerror", "?"),
            getattr(e, "filename", "?"),
            getattr(e, "strerror", "?"),
            os.getcwd(),
            os.environ.get("PATH", "")[:200],
        )
        raise FFmpegError(
            f"ffmpeg not found in PATH "
            f"(attempted: {cmd[0]!r}, exists={Path(cmd[0]).is_file()}, "
            f"winerror={getattr(e, 'winerror', '?')}, "
            f"filename={getattr(e, 'filename', '?')!r}, "
            f"strerror={getattr(e, 'strerror', '?')!r})"
        ) from e

    with registered_process(process):
        memory_cancelled = threading.Event()

        def _memory_cancel_callback() -> bool:
            memory_cancelled.set()
            return True

        def _effective_cancel_callback() -> bool:
            if memory_cancelled.is_set():
                return True
            return bool(cancel_callback and cancel_callback())

        if memory_monitor is not None:
            # Late-bind the pid now that the process exists, then start
            # the monitor thread. The monitor reads RSS by pid, so this
            # must happen after Popen returns.
            memory_monitor.pid = process.pid
            memory_monitor.cancel_callback = _memory_cancel_callback
            memory_monitor.start()
        stderr_pipe = process.stderr
        assert stderr_pipe is not None
        stderr_lines: list[str] = []
        wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
        drain_done = False
        last_progress_time = time.monotonic()

        # P1.5: stall watchdog. The ``track_progress=True`` branch checks
        # ``elapsed_since_progress`` inside its readline loop, but readline
        # blocks until ffmpeg emits a line -- a fully-hung ffmpeg (deadlock,
        # no stdout at all) would never surface as a stall there. This
        # daemon thread polls ``last_progress_time`` independently of
        # stdout availability and kills the process when the stall window
        # expires. The track_progress loop's inline check is retained as
        # a fast path (it kills ASAP after a stalled line arrives).
        #
        # ``stall_killed`` is set BEFORE the kill() so the post-mortem
        # rc-analysis can distinguish "watchdog killed a stalled ffmpeg"
        # from a genuine OOM/SIGKILL (rc -9). Without this, a stall-kill
        # surfaced as rc=-9 to the main loop and looked_like_oom reported
        # "ran out of memory" — the user then chased memory instead of
        # the real cause (P1 audit v0.3 §4).
        stall_stop = threading.Event()
        stall_killed = threading.Event()

        def _stall_watchdog() -> None:
            while not stall_stop.wait(CANCEL_POLL_INTERVAL):
                if process.poll() is not None:
                    return
                elapsed = time.monotonic() - last_progress_time
                if elapsed > stall_kill:
                    logger.error(
                        f"{label}: stall watchdog firing -- no progress for "
                        f"{int(elapsed)}s, killing process"
                    )
                    stall_killed.set()
                    try:
                        process.kill()
                    except Exception:
                        logger.exception("stall watchdog: kill() failed")
                    return

        stall_thread = threading.Thread(target=_stall_watchdog, daemon=True, name=f"stall_{label}")
        stall_thread.start()

        try:
            with cancel_monitor(process, _effective_cancel_callback) as cancelled:
                if track_progress:
                    stdout_pipe = process.stdout
                    assert stdout_pipe is not None
                    # P1.5: use a queue-based reader so the consumer loop
                    # can check cancel / stall between reads without
                    # blocking on readline(). A hung ffmpeg that stops
                    # emitting stdout would block readline() forever;
                    # the queue + get(timeout=...) lets the inline stall
                    # check run even when no new lines arrive.
                    line_queue, _reader_thread = read_lines_queue(stdout_pipe)
                    while True:
                        try:
                            raw_line = line_queue.get(timeout=CANCEL_POLL_INTERVAL)
                        except queue.Empty:
                            # No new line -- check cancel + stall.
                            if _effective_cancel_callback():
                                process.kill()
                                raise CancelledError(f"{label} cancelled") from None
                            if cancelled.is_set():
                                raise CancelledError(f"{label} cancelled") from None
                            elapsed_since_progress = time.monotonic() - last_progress_time
                            if elapsed_since_progress > stall_kill:
                                process.kill()
                                raise FFmpegError(
                                    f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                    "possible resource exhaustion"
                                ) from None
                            elif elapsed_since_progress > stall_warning:
                                logger.warning(
                                    f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                                )
                            continue
                        if raw_line is None:
                            break  # EOF -- pipe closed
                        if _effective_cancel_callback():
                            process.kill()
                            raise CancelledError(f"{label} cancelled")
                        if cancelled.is_set():
                            raise CancelledError(f"{label} cancelled")
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line.startswith("out_time_us="):
                            last_progress_time = time.monotonic()
                            if progress_callback:
                                try:
                                    us = int(line.split("=", 1)[1])
                                    progress_callback(us / 1_000_000)
                                except (ValueError, IndexError):
                                    pass
                        elapsed_since_progress = time.monotonic() - last_progress_time
                        if elapsed_since_progress > stall_kill:
                            process.kill()
                            raise FFmpegError(
                                f"{label} stalled -- no progress for {int(elapsed_since_progress)}s, "
                                "possible resource exhaustion"
                            )
                        elif elapsed_since_progress > stall_warning:
                            logger.warning(
                                f"{label}: no progress for {int(elapsed_since_progress)}s -- waiting..."
                            )

                if cancelled.is_set():
                    raise CancelledError(f"{label} cancelled")
                _wait_with_cancel(process, timeout, _effective_cancel_callback, label)
                wait_for_drain()
                drain_done = True

                if process.returncode != 0:
                    stderr_text = "".join(stderr_lines)
                    msg = (
                        stderr_text[:_STDERR_TRUNCATE]
                        if stderr_text
                        else "unknown error (no stderr)"
                    )
                    # Memory monitor's hard-budget kill: it triggers cancel
                    # via cancel_callback rather than killing the process
                    # directly, and on a race the cancel_monitor's kill can
                    # land before cancelled propagates — so we'd otherwise
                    # reach the rc != 0 branch with a SIGKILL and report
                    # this as a stall or a generic ffmpeg failure. Surface
                    # it as an OOM-class error here so the user sees the
                    # "lower the budget" hint (P1 audit v0.3 §4).
                    if memory_monitor is not None and memory_monitor.hard_exceeded:
                        raise FFmpegOutOfMemoryError(
                            f"{label} ran out of memory "
                            f"(memory monitor hard limit hit, "
                            f"rc={process.returncode}); "
                            "try --preset low_memory / lowering "
                            "--memory-limit-mb / reducing --batch-chunk-size"
                        )
                    # Stall-watchdog kill (rc=-9 on POSIX): distinguish from
                    # a real OOM kill BEFORE looks_like_oom claims it (P1
                    # audit v0.3 §4). The watchdog set the flag just before
                    # process.kill(); the inline stall-check in the reader
                    # loop also raises a stall FFmpegError directly, but a
                    # race between reader EOF and the watchdog firing could
                    # surface rc=-9 — so the flag check is the source of
                    # truth here.
                    if stall_killed.is_set():
                        raise FFmpegError(
                            f"{label} stalled -- no progress for > {stall_kill}s, "
                            "process killed by watchdog"
                        )
                    # P3.x: surface OOM as a dedicated error so the CLI/GUI
                    # can hint the user to lower the memory budget or pick
                    # the Low-memory preset, instead of dumping the raw
                    # stderr. SIGKILL on POSIX (rc -9 / 137) or stderr
                    # allocator-failure markers — see looks_like_oom.
                    if looks_like_oom(process.returncode, stderr_text):
                        raise FFmpegOutOfMemoryError(
                            f"{label} ran out of memory "
                            f"(rc={process.returncode}); "
                            "try --preset low_memory / lowering "
                            "--memory-limit-mb / reducing --batch-chunk-size"
                        )
                    raise FFmpegError(f"{label} failed: {msg}")

        except CancelledError:
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory "
                    "(memory monitor hard limit hit); "
                    "try --preset low_memory / lowering --memory-limit-mb / "
                    "reducing --batch-chunk-size"
                ) from None
            raise
        except subprocess.TimeoutExpired as e:
            process.kill()
            if memory_monitor is not None and memory_monitor.hard_exceeded:
                raise FFmpegOutOfMemoryError(
                    f"{label} ran out of memory "
                    "(memory monitor hard limit hit); "
                    "try --preset low_memory / lowering --memory-limit-mb / "
                    "reducing --batch-chunk-size"
                ) from None
            raise FFmpegError(f"{label} timeout after {e.timeout}s") from None
        finally:
            stall_stop.set()
            if memory_monitor is not None:
                memory_monitor.stop()
                # Surface the peak RSS so the user can see how close they
                # came to the budget. Logged at INFO (always visible) when
                # the monitor saw any progress; debug otherwise.
                if memory_monitor.peak_rss_mb > 0:
                    logger.info(
                        f"{label}: peak RSS {memory_monitor.peak_rss_mb:.0f}MB"
                        + (
                            " (HARD limit hit -- task cancelled)"
                            if memory_monitor.hard_exceeded
                            else ""
                        )
                        + (
                            " (OS reserve was breached -- warning only)"
                            if getattr(memory_monitor, "os_reserve_breached", False)
                            else ""
                        )
                    )
            if not drain_done:
                wait_for_drain()
            if process.stdout is not None:
                process.stdout.close()
            stderr_pipe.close()


def _run_subprocess_cmd(
    cmd: list[str],
    *,
    timeout: int,
    label: str,
    cancel_callback: Callable[[], bool] | None = None,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
) -> None:
    """Run a single ffmpeg command with timeout / cancel / registration.

    Minimal sibling of ``_run_ffmpeg`` for commands that don't emit a
    -progress stream (e.g. the stream-copy "cut" phase in
    ``_run_cut_then_encode``). Unlike the historical bare
    ``subprocess.run(check=True, capture_output=True)`` this:

      * registers the process in the scoped supervisor so Cancel-GUI /
        on-close kill reaches the running ffmpeg (P0 audit v0.3 §3);
      * polls ``cancel_callback`` during the wait (not just between
        segments) so a cancel mid-segment fires immediately;
      * bounds the run with ``timeout`` so a hung ffmpeg doesn't hang
        the whole pipeline;
      * wraps ``CalledProcessError`` / ``TimeoutExpired`` in a
        ``ConcatError`` carrying a truncated stderr so the CLI/GUI
        surfaces a friendly message instead of a raw traceback.

    Stderr is collected from the pipe via ``drain_stderr_lines`` and
    surfaced on error. No progress callback — cut-фаза caller uses the
    segment index for progress.
    """
    try:
        process = popen_with_retry(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **subprocess_kwargs(low_process_priority, rlimit_as_mb),
        )
    except FileNotFoundError as e:
        raise FFmpegError(
            f"ffmpeg not found in PATH "
            f"(attempted: {cmd[0]!r}, winerror={getattr(e, 'winerror', '?')}, "
            f"filename={getattr(e, 'filename', '?')!r})"
        ) from e

    stderr_pipe = process.stderr
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False
    try:
        with registered_process(process), cancel_monitor(process, cancel_callback) as cancelled:
            if cancelled.is_set():
                raise CancelledError(f"{label} cancelled")
            _wait_with_cancel(process, timeout, cancel_callback, label)
            if cancelled.is_set():
                raise CancelledError(f"{label} cancelled")
            wait_for_drain()
            drain_done = True
            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                if looks_like_oom(process.returncode, stderr_text):
                    raise FFmpegOutOfMemoryError(
                        f"{label} ran out of memory (rc={process.returncode}); "
                        "try --preset low_memory / lowering --memory-limit-mb"
                    )
                msg = stderr_text[:_STDERR_TRUNCATE] if stderr_text else "unknown error (no stderr)"
                raise ConcatError(f"{label} failed (rc={process.returncode}): {msg}")
    except subprocess.TimeoutExpired as e:
        process.kill()
        raise FFmpegError(f"{label} timeout after {e.timeout}s") from None
    finally:
        if not drain_done:
            wait_for_drain()
        stderr_pipe.close()


def _wait_with_cancel(
    process: subprocess.Popen,
    timeout: int,
    cancel_callback: Callable[[], bool] | None,
    label: str,
) -> int:
    """Poll process.wait() so cancel_callback is checked periodically.

    Returns the returncode, or raises CancelledError / TimeoutExpired.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            return process.wait(timeout=min(CANCEL_POLL_INTERVAL, remaining))
        except subprocess.TimeoutExpired:
            if cancel_callback and cancel_callback():
                process.kill()
                raise CancelledError(f"{label} cancelled") from None


# ---------------------------------------------------------------------------
# Resume manifest (P0.6)
# ---------------------------------------------------------------------------
# A manifest is written next to the segment/batch working directory the
# first time a run starts. On resume, the manifest is loaded and validated
# against the current run's parameters (source identity + encoder +
# quality + pipeline version + keep segments). A mismatch invalidates the
# working dir so old artifacts from an incompatible run cannot be reused.
#
# Source identity is (path, size, mtime_ns). A full hash is intentionally
# avoided -- for a 6h stream that's an extra O(filesize) read. (path,
# size, mtime_ns) is enough to detect: file moved/replaced, file edited
# in place (size and mtime change), file re-encoded (size and mtime
# change). For adversarial cases (size+mtime preserved by external
# tooling) the user can pass --force.

PIPELINE_VERSION = 3  # bump when the on-disk segment/chunk format changes


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / "_manifest.json"


def _source_identity(video_path: Path) -> dict:
    """Snapshot (path, size, mtime_ns) so resume detects source changes.

    Uses the absolute path so renaming the file (same content) doesn't
    silently reuse segments encoded against a different filename -- the
    concat list references segments by their position in the run, so a
    path rename invalidates the work dir deliberately.
    """
    st = video_path.stat()
    return {
        "path": str(video_path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def _build_manifest(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    method: str,
    encoder: str,
    vcodec: str,
    vcodec_opts: list[str],
    video_quality: str,
    audio_quality: str,
    x264_preset: str,
    encoder_threads: str | int,
) -> dict:
    """Construct the manifest dict describing the current run's identity."""
    return {
        "pipeline_version": PIPELINE_VERSION,
        "source": _source_identity(video_path),
        "method": method,
        "encoder": encoder,
        "resolved_encoder": vcodec,  # may differ from `encoder` after fallback
        "encoder_opts": list(vcodec_opts),
        "video_quality": video_quality,
        "audio_quality": audio_quality,
        "x264_preset": x264_preset,
        "encoder_threads": encoder_threads,
        "keep_segments": list(keep_segments),
        "keep_segments_total_duration": sum(e - s for s, e in keep_segments),
        "created_at": time.time(),
    }


def _write_manifest(work_dir: Path, manifest: dict) -> None:
    """Atomically write the manifest to ``work_dir/_manifest.json``.

    The temp file is created in the same directory so os.replace is atomic
    on the same filesystem. Parent exists because the caller already
    mkdir'd the work dir.
    """
    manifest_path = _manifest_path(work_dir)
    fd, tmp_path = tempfile.mkstemp(dir=work_dir, prefix=f".{manifest_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, manifest_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_manifest(work_dir: Path) -> dict | None:
    """Read the manifest at ``work_dir/_manifest.json`` or return None."""
    manifest_path = _manifest_path(work_dir)
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Resume manifest at {manifest_path} is unreadable: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _validate_manifest(
    work_dir: Path,
    current: dict,
) -> bool:
    """Return True if the on-disk manifest matches the current run identity.

    A mismatch means the existing segments/chunks were produced by a
    different run (different source, encoder, quality, keep segments,
    or pipeline version) and must not be reused. Caller is responsible
    for wiping the work dir on False.
    """
    stored = _load_manifest(work_dir)
    if stored is None:
        # No manifest = the work dir predates the manifest system (or was
        # created by a crash before the manifest was written). Be safe
        # and treat as invalid.
        logger.info(f"Resume: no manifest in {work_dir}, treating as stale")
        return False
    for key in (
        "pipeline_version",
        "method",
        "encoder",
        "resolved_encoder",
        "encoder_opts",
        "video_quality",
        "audio_quality",
        "x264_preset",
        "encoder_threads",
    ):
        if stored.get(key) != current.get(key):
            logger.info(
                f"Resume: manifest mismatch on {key}: "
                f"stored={stored.get(key)!r} current={current.get(key)!r}"
            )
            return False
    # Source identity (path/size/mtime) -- strict equality.
    stored_src = stored.get("source") or {}
    current_src = current.get("source") or {}
    for key in ("path", "size", "mtime_ns"):
        if stored_src.get(key) != current_src.get(key):
            logger.info(
                f"Resume: source {key} changed: "
                f"stored={stored_src.get(key)!r} current={current_src.get(key)!r}"
            )
            return False
    # Keep segments must match exactly -- different cut points = different
    # output. Stored segments were written by json.dump (lists of lists);
    # compare as tuples for float safety.
    stored_segs = stored.get("keep_segments") or []
    current_segs = current.get("keep_segments") or []
    if len(stored_segs) != len(current_segs):
        logger.info(
            f"Resume: keep_segments length differs ({len(stored_segs)} vs {len(current_segs)})"
        )
        return False
    for (s1, e1), (s2, e2) in zip(stored_segs, current_segs, strict=True):
        if abs(float(s1) - float(s2)) > 1e-9 or abs(float(e1) - float(e2)) > 1e-9:
            logger.info(f"Resume: keep_segments differ: {stored_segs} vs {current_segs}")
            return False
    return True


def _ensure_fresh_work_dir(
    work_dir: Path,
    current_manifest: dict,
) -> None:
    """Validate ``work_dir`` against ``current_manifest``; wipe if stale.

    The wipe is destructive -- it removes the entire work dir including
    partial segments. Call this BEFORE the resume-skip loop in
    ``_run_segment_concat`` / ``_run_batch_concat`` so the per-file
    checks see only artifacts that belong to the current run.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if _validate_manifest(work_dir, current_manifest):
        return
    if work_dir.exists() and any(work_dir.iterdir()):
        logger.info(
            f"Resume: invalidating work dir {work_dir} "
            f"(manifest mismatch or missing); re-encoding from scratch"
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(work_dir, current_manifest)


def _ffprobe_is_valid_media(path: Path, stream_type: str = "v") -> bool:
    """Quick validity check: ffprobe can read codec + duration for the
    requested stream type.

    Used by resume-skip to reject a chunk that exists and is large enough
    but is internally corrupt (e.g. ffmpeg crashed mid-write and the
    moov atom is missing). Without this, the concat demuxer would accept
    the file but emit a broken segment in the middle of the output.

    ``stream_type`` selects the ffprobe ``-select_streams`` filter: ``"v"``
    for video segments (the historical default, used by the concat
    segment/cut/raw paths) and ``"a"`` for audio segments (audio-extract
    resume — an audio-only file has no video stream and would otherwise
    fail video validation → resume always re-encoded everything, see
    the P0 audit in the v0.3 release plan).
    """
    try:
        r = run_with_retry(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-select_streams",
                stream_type,
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **no_window_kwargs(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


# Back-compat alias for the old name; new call sites should use
# _ffprobe_is_valid_media(path, stream_type=...). Kept so external
# search/grep across older branches doesn't report a dangling reference.
def _ffprobe_is_valid_mp4(path: Path) -> bool:
    return _ffprobe_is_valid_media(path, stream_type="v")


def _ffprobe_duration_ok(path: Path, expected_seconds: float, *, slack: float = 1.0) -> bool:
    """Check that a resume part's ffprobe duration is close to the expected value.

    ffmpeg killed mid-write can leave a valid moov atom (the file passes
    ``_ffprobe_is_valid_media``) but a truncated body — the duration read
    from the moov reflects the planned length, not the actual content. Comparing
    against the expected duration catches holes in the middle of the final
    video. ``slack`` is the tolerance in seconds; 1.0s covers encoder flush
    jitter and ffmpeg's own rounding without accepting truncated outputs.

    When ffprobe cannot determine the duration (corrupt file, timeout,
    non-media data), returns ``True`` — the caller's existing
    ``_ffprobe_is_valid_media`` codec check already gatekeeps those cases,
    and we don't want to double-reject a file whose codec is fine but
    whose duration is unreadable for unrelated reasons.
    """
    try:
        r = run_with_retry(
            [
                ffprobe_path(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            **no_window_kwargs(),
        )
        if r.returncode != 0:
            return True  # duration unreadable — fall back to codec check alone
        duration_str = r.stdout.strip()
        if not duration_str:
            return True
        actual = float(duration_str)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return True  # duration unreadable — fall back to codec check alone
    return abs(actual - expected_seconds) <= slack


def _run_final_concat(
    work_dir: Path,
    output_path: Path,
    part_paths: list[Path],
    *,
    total_duration: float,
    progress_callback: Callable[[float], None] | None,
    cancel_callback: Callable[[], bool] | None,
    label: str,
    timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Build ``concat.txt`` and run the final concat-demuxer pass.

    Shared by ``_run_segment_concat`` and ``_run_batch_concat`` (P2.6).
    Both methods previously had identical 30-line blocks here: open
    ``concat.txt``, write one ``file <name>`` line per part, run
    ``ffmpeg -fflags +genpts -f concat -safe 0 -i ... -c copy``,
    cleanup. The only real differences were the part filename pattern
    (``seg_NNNNNN.mp4`` vs ``chunk_NNNN.mp4``) and the label string;
    both are now parameters so the body lives once.

    The progress callback maps ffmpeg's ``out_time_us`` (which reflects
    output time across the whole concat, not per-segment) to the last
    10% of the overall progress bar -- both call sites reserve 0..0.9
    for the per-segment encodes and 0.9..1.0 for this final concat.
    """
    list_path = work_dir / "concat.txt"
    with open(list_path, "w", encoding="utf-8") as lf:
        for part in part_paths:
            lf.write(f"file {_quote_concat_path(part.name)}\n")

    def _concat_prog(seconds: float) -> None:
        if progress_callback and total_duration > 0:
            progress_callback(min(seconds / total_duration * 0.1, 0.1) + 0.9)

    label_text = label
    _run_ffmpeg(
        [
            ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            # -fflags +genpts is a *demuxer* flag, so it goes BEFORE -i,
            # not as an output option after -i (P1 audit v0.3 §5.3). It
            # tells the concat demuxer to generate missing PTS values
            # for packets whose timestamps got dropped/duplicated at the
            # segment boundaries. As an output option (the historical
            # position after -i) it was effectively ignored for the PTS
            # rebuild contract — the muxer honoured it only on its own
            # output writes, which fire AFTER the demuxer has already
            # assembled the packet stream and parsed (or missed) PTS.
            "-fflags",
            "+genpts",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ],
        progress_callback=_concat_prog,
        timeout=timeout,
        label=label_text,
        cancel_callback=cancel_callback,
        memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
        stall_kill=stall_kill,
        stall_warning=stall_warning,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
    )


def _run_audio_concat_filter(
    output_path: Path,
    part_paths: list[Path],
    *,
    codec: str,
    audio_quality: str,
    extra_opts: list[str],
    total_duration: float,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Join audio parts via the ``concat`` filter (re-encode path).

    Used for containers whose muxer misreports duration on a concat-
    demuxer join (notably ``.flac`` — ffmpeg's flac muxer keeps the
    first segment's duration when stream-copying concat-demuxer input,
    producing a 2s file from two 2s segments). The concat filter
    decodes every part into PCM, concatenates the PCM buffers, and
    re-encodes once — reliable but not free.

    For lossless codecs (flac, wav) the re-encode round-trips
    bit-exact, so the lossless contract is preserved. Lossy codecs
    shouldn't reach here (the caller routes them through the concat
    demuxer instead), but if they did, the re-encode would add one
    generation of loss — acceptable as a fallback, not as policy.
    """
    n = len(part_paths)
    if n == 0:
        raise ConcatError("audio concat filter: no parts to join")

    # Build the -i inputs and the [N:a]concat=n=N:v=0:a=1 filter graph.
    inputs: list[str] = []
    for p in part_paths:
        inputs.extend(["-i", str(p)])
    chain = "".join(f"[{i}:a]" for i in range(n))
    graph = f"{chain}concat=n={n}:v=0:a=1[outa]"

    def _concat_prog(seconds: float) -> None:
        if progress_callback and total_duration > 0:
            progress_callback(min(seconds / total_duration * 0.1, 0.1) + 0.9)

    label_text = "audio concat filter"
    _run_ffmpeg(
        [
            ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[outa]",
            "-c:a",
            codec,
            *_audio_opts(audio_quality),
            *extra_opts,
            str(output_path),
        ],
        progress_callback=_concat_prog,
        timeout=timeout,
        label=label_text,
        cancel_callback=cancel_callback,
        memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
        stall_kill=stall_kill,
        stall_warning=stall_warning,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
    )


def _run_audio_extract(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    output_format: str,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    audio_quality: str = "medium",
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    min_part_bytes: int = _MIN_PART_BYTES,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Extract audio-only output (mp3/opus/aac/wav/flac) from keep segments.

    The video stream is dropped entirely. Each keep segment is decoded
    via input-side ``-ss`` / ``-t`` (frame-accurate, same approach as
    ``_run_segment_concat``) and re-encoded into the chosen audio
    codec; the per-segment files are joined by the concat demuxer
    (lossless stream-copy join, no second encode pass).

    Resume: same manifest mechanism as the other paths — a mismatch in
    source / codec / quality / keep_segments wipes the work dir; each
    part file is ffprobe-validated so a partial crash artifact isn't
    reused.

    The per-segment encode is the *only* lossy step (for mp3/opus/aac);
    the concat demuxer join is lossless. AAC priming (~21 ms per
    segment) accumulates slightly across segments, mirroring the
    segment path's documented behaviour. For lossless formats (wav,
    flac) the priming is zero and the output is sample-accurate.

    ``audio_quality`` controls the bitrate for lossy formats via
    ``_audio_bitrate_opts()`` (high=256k, medium=192k, low=128k);
    ``source`` and wav/flac omit the bitrate knob.
    """
    spec = OUTPUT_FORMAT_SPECS.get(output_format)
    if spec is None:
        # Should be unreachable: cut_and_concat validates output_format
        # before dispatching here. Kept as a defensive guard so a future
        # caller that bypasses cut_and_concat gets a clear error.
        raise ConcatError(f"Unknown output_format {output_format!r}")
    codec = spec["codec"]
    ext = spec["ext"]
    lossless = bool(spec["lossless"])
    extra_opts: list[str] = list(spec["extra_opts"])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(
        f"audio_extract ({output_format}): {n_segs} segments, "
        f"{total_duration:.1f}s output, codec={codec}"
    )

    work_dir = output_path.parent / f"_{output_path.stem}_audio_{ext}"
    manifest = _build_manifest(
        video_path,
        keep_segments,
        "audio_extract",
        output_format,  # encoder slot — the audio "format" identifies the run
        codec,  # vcodec slot — the actual ffmpeg codec used
        [],  # vcodec_opts slot — audio has no encoder opts beyond -c:a
        "n/a",  # video_quality slot — not applicable to audio-only
        audio_quality,
        "n/a",  # x264_preset slot
        "auto",  # encoder_threads slot
    )
    _ensure_fresh_work_dir(work_dir, manifest)

    # Bitrate knob: only meaningful for lossy codecs. For wav/flac the
    # encoder ignores -b:a anyway, but we omit it to keep the ffmpeg
    # command line readable in the log.
    bitrate_opts: list[str] = []
    if not lossless:
        bitrate_opts = _audio_bitrate_opts(audio_quality)

    try:
        encoded_keep = 0.0
        skipped = 0

        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise CancelledError("audio extract cancelled")

            dur = end - start
            seg_path = work_dir / f"seg_{i:06d}.{ext}"

            # Resume: skip already encoded segments. Same dual check as
            # _run_segment_concat: minimum size + ffprobe validity.
            # Audio segments use stream_type="a" — a video-stream probe
            # would reject any valid mp3/opus/aac/wav/flac chunk because
            # it has no video stream, defeating resume (P0 audit v0.3).
            if (
                seg_path.exists()
                and seg_path.stat().st_size >= min_part_bytes
                and _ffprobe_is_valid_media(seg_path, stream_type="a")
                and _ffprobe_duration_ok(seg_path, dur)
            ):
                skipped += 1
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))
                continue

            def _seg_prog(
                seconds: float,
                _dur: float = dur,
                _encoded_keep: float = encoded_keep,
            ) -> None:
                # Map ffmpeg's per-segment out_time_us to absolute
                # progress across the whole output, same trick as the
                # segment path's _seg_prog. The 0.9 ceiling leaves room
                # for the final concat pass.
                if progress_callback and total_duration > 0 and _dur > 0:
                    seg_frac = min(seconds / _dur, 1.0)
                    abs_time = _encoded_keep + seg_frac * _dur
                    progress_callback(min(abs_time / total_duration * 0.9, 0.9))

            label_text = f"audio segment {i} encode"
            _run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-y",
                    "-loglevel",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{dur:.3f}",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    codec,
                    *bitrate_opts,
                    *_audio_opts(audio_quality),
                    *extra_opts,
                    str(seg_path),
                ],
                progress_callback=_seg_prog,
                timeout=segment_encode_timeout,
                label=label_text,
                cancel_callback=cancel_callback,
                memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
            )

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        if skipped:
            logger.info(
                f"audio_extract: resumed {skipped}/{n_segs} already encoded, "
                f"encoded {n_segs - skipped}"
            )

        # Final join pass. Two strategies, picked by container:
        #
        #   * **concat demuxer** (mp3 / opus / aac-m4a / wav) — stream-
        #     copies the per-segment audio into one file, no re-encode,
        #     so lossy priming isn't re-added. Works because these
        #     containers' muxers honour the concat demuxer's "concatenate
        #     packets in order" semantics.
        #
        #   * **concat filter** (flac) — re-encodes through a single
        #     filter graph. The flac muxer misreports duration on a
        #     concat-demuxer join (it keeps the first segment's
        #     duration), so the lossless re-encode is the only reliable
        #     path. For flac the re-encode is lossless (flac → PCM → flac
        #     round-trips bit-exact), so this doesn't violate the
        #     "lossless" contract of the format.
        #
        # WAV would also work with the concat filter, but the demuxer
        # path is faster (no re-encode) and verified correct, so WAV
        # stays on the demuxer path.
        part_paths = [work_dir / f"seg_{i:06d}.{ext}" for i in range(n_segs)]
        if ext == "flac":
            _run_audio_concat_filter(
                output_path,
                part_paths,
                codec=codec,
                audio_quality=audio_quality,
                extra_opts=extra_opts,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )
        else:
            _run_final_concat(
                work_dir,
                output_path,
                part_paths,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                label="audio extract concat",
                timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )
        logger.info(f"Successfully created audio output: {output_path}")

        shutil.rmtree(work_dir, ignore_errors=True)

    except Exception:
        logger.info(f"Audio segments kept in {work_dir} for resume on next run")
        raise


def _run_gapless_segment_concat(
    output_path: Path,
    part_paths: list[Path],
    vcodec: str,
    vcodec_opts: list[str],
    *,
    audio_quality: str = "medium",
    total_duration: float,
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Gapless segment join via concat filter (re-encode both streams).

    The concat demuxer stream-copies per-segment AAC, preserving each
    segment's encoder priming (~21ms at 48kHz) — N segments drift
    ~21*N ms. The concat filter decodes every segment's audio into PCM,
    concatenates the PCM buffers, and re-encodes once, so priming is
    added only once (not per-segment).

    Video is also re-encoded through the concat filter (``v=1:a=1``).
    This is the trade-off of gapless_concat: the video quality loss is
    one generation (H.264 → decode → H.264), but the output is truly
    gapless (no per-segment priming on either stream). For lossless
    video + gapless audio, use ``cut_then_encode`` instead — it does
    one encode pass total, but sacrifices frame accuracy (``-c copy``
    snaps to keyframes).

    The command shape is::

        ffmpeg -i seg_0.mp4 -i seg_1.mp4 ... -i seg_N.mp4 \\
               -filter_complex "[0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[outv][outa]" \\
               -map "[outv]" -map "[outa]" \\
               -c:v <vcodec> <vcodec_opts> -c:a aac -b:a <bitrate> \\
               -ar 48000 -ac 2 -movflags +faststart \\
               output.mp4

    The ``-filter_complex`` graph interleaves video and audio pads
    (``[0:v][0:a][1:v][1:a]...``) because the concat filter expects
    them in that order, not all-videos-then-all-audios.
    """
    n = len(part_paths)
    if n == 0:
        raise ConcatError("gapless concat: no parts to join")

    inputs: list[str] = []
    for p in part_paths:
        inputs.extend(["-i", str(p)])
    # concat filter expects interleaved [v0][a0][v1][a1]... ordering.
    chain = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    graph = f"{chain}concat=n={n}:v=1:a=1[outv][outa]"

    def _concat_prog(seconds: float) -> None:
        if progress_callback and total_duration > 0:
            progress_callback(min(seconds / total_duration * 0.1, 0.1) + 0.9)

    label_text = "gapless segment concat"
    _run_ffmpeg(
        [
            ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            vcodec,
            *vcodec_opts,
            "-c:a",
            "aac",
            *_audio_bitrate_opts(audio_quality),
            *_audio_opts(audio_quality),
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        progress_callback=_concat_prog,
        timeout=timeout,
        label=label_text,
        cancel_callback=cancel_callback,
        memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
        stall_kill=stall_kill,
        stall_warning=stall_warning,
        low_process_priority=low_process_priority,
        rlimit_as_mb=rlimit_as_mb,
    )


def _run_segment_concat(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: list[str],
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    encoder: str = "libx264",
    video_quality: str = "medium",
    audio_quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    source_has_audio: bool = True,
    output_fps: str = "source",
    gapless_concat: bool = False,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    min_part_bytes: int = _MIN_PART_BYTES,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Encode each segment, join with concat demuxer (or concat filter for gapless).

    Segments are stored in a dedicated subdirectory.  If a previous run was
    interrupted, already-encoded segments are reused (resume from where it
    stopped).  On success all segment files are deleted.

    Resume integrity (P0.6): the work dir contains a ``_manifest.json``
    snapshot of (source path/size/mtime, encoder, encoder_opts, quality,
    keep_segments, pipeline_version). A mismatch wipes the work dir so
    old artifacts from an incompatible run cannot be reused. Each
    resumed segment is also ffprobe-validated so a partial moov-atom
    crash artifact is detected and re-encoded.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    n_segs = len(keep_segments)
    logger.info(f"segment: {n_segs} segments, {total_duration:.1f}s output, {vcodec}")

    seg_dir = output_path.parent / f"_{output_path.stem}_segments"
    manifest = _build_manifest(
        video_path,
        keep_segments,
        "segment",
        encoder,
        vcodec,
        vcodec_opts,
        video_quality,
        audio_quality,
        x264_preset,
        encoder_threads,
    )
    _ensure_fresh_work_dir(seg_dir, manifest)

    encoded_keep = 0.0
    skipped = 0

    try:
        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise CancelledError("segment encode cancelled")

            dur = end - start
            seg_path = seg_dir / f"seg_{i:06d}.mp4"

            # Resume: skip already encoded segments. Require both a
            # minimum size AND a successful ffprobe read so a crash
            # artifact (missing moov atom) doesn't get reused and
            # corrupt the final concat in the middle.
            if (
                seg_path.exists()
                and seg_path.stat().st_size >= min_part_bytes
                and _ffprobe_is_valid_mp4(seg_path)
            ):
                skipped += 1
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))
                continue

            # Frame-accurate segment encode using a SINGLE input-side seek.
            #
            # The earlier pipeline used both an input-side `-ss` (coarse,
            # keyframe-aligned fast seek) AND an output-side `-ss` for an
            # additional sub-keyframe correction. Two consecutive seeks on
            # the same input produce an off-by-~0.5s systematic bias that
            # cuts the start of every segment after t≈0.5s, and combined
            # with the `setpts=N/FRAME_RATE/TB` resync it also dropped
            # frames at the boundary (verified: a 6s/30FPS source with
            # keep=[(0,2),(3,5)] produced 4.72s/135 frames instead of
            # the expected 4.00s/120).
            #
            # The current approach:
            #   1. Input-side `-ss {start}` performs the seek. ffmpeg's
            #      MP4 demuxer decodes from the preceding keyframe and
            #      drops frames until `start` automatically -- this is
            #      frame-accurate on modern ffmpeg builds (verified with
            #      ffmpeg 8.1.1 on a GOP=30 source at a sub-keyframe
            #      cut: a 1.5s keep returned exactly 1.500s/45 frames).
            #   2. `-t {dur}` (output-side duration on the WHOLE output)
            #      limits both video and audio to exactly `dur`. No extra
            #      `apad`/`atrim` is needed: audio is also bound by
            #      `-t`, so no per-segment `_AUDIO_PAD` drift accumulates.
            #   3. setpts/atrim are unnecessary: the input PTS is already
            #      in source time, and after `-ss`+`-t` the segment's
            #      output starts at t=0 by ffmpeg's normalisation (genpts
            #      by the muxer). The concat demuxer handles the join.
            #
            # `-copyts` is NOT used: kept off so the per-segment output
            # timeline starts at 0 (the contract the concat demuxer
            # expects when ``-fflags +genpts`` (demuxer-side, placed
            # before ``-i``) rebuilds the final PTS).
            # Without `-copyts`, timestamps in the segment file are
            # already normalised to start at 0, so a `setpts=PTS-STARTPTS`
            # is a no-op here and is omitted for clarity.

            def _seg_prog(seconds: float, _dur: float = dur, _encoded_keep: float = encoded_keep) -> None:
                # ffmpeg -progress reports `out_time_us` -- the position within
                # this segment's output, NOT the original video. Map it to
                # absolute progress across the whole video so the GUI/CLI
                # bar moves smoothly even when a single segment takes an
                # hour (e.g. 0 silence segments → 1 keep segment = the
                # whole video).
                if progress_callback and total_duration > 0 and _dur > 0:
                    seg_frac = min(seconds / _dur, 1.0)
                    abs_time = _encoded_keep + seg_frac * _dur
                    progress_callback(min(abs_time / total_duration * 0.9, 0.9))

            label_text = f"segment {i} encode"
            _run_ffmpeg(
                [
                    ffmpeg_path(),
                    "-y",
                    "-loglevel",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{dur:.3f}",
                    # Explicit stream mapping (P1.14): pick the first
                    # video stream and the first audio stream rather
                    # than letting ffmpeg auto-select. A source with
                    # multiple audio tracks (e.g. dual-language MKV)
                    # would otherwise have its track choice depend on
                    # ffmpeg's stream-order heuristic, which isn't
                    # stable across versions. When the source has no
                    # audio, audio mapping and the AAC encoder are
                    # omitted entirely so the segment encode produces
                    # a valid video-only MP4 instead of failing with
                    # "Output file does not contain any stream".
                    "-map",
                    "0:v:0",
                    *(
                        # P1.17: when the user requests a CFR target
                        # (output_fps != "source"), apply the ``fps``
                        # filter on the video stream. Without a filter
                        # graph the ``-r`` output option would work
                        # too, but the filter is the documented way
                        # to do it post-encode PTS normalisation and
                        # matches the batch path's filter chain shape.
                        ["-vf", f"fps={output_fps}"] if output_fps != "source" else []
                    ),
                    "-c:v",
                    vcodec,
                    *vcodec_opts,
                    *(
                        [
                            "-map",
                            "0:a:0?",
                            "-c:a",
                            "aac",
                            *_audio_bitrate_opts(audio_quality),
                            *_audio_opts(audio_quality),
                        ]
                        if source_has_audio
                        else []
                    ),
                    str(seg_path),
                ],
                progress_callback=_seg_prog,
                timeout=segment_encode_timeout,
                label=label_text,
                cancel_callback=cancel_callback,
                memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
            )

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        if skipped:
            logger.info(
                f"segment: resumed {skipped}/{n_segs} already encoded, encoded {n_segs - skipped}"
            )

        # Final join. Two strategies, picked by the ``gapless_concat``
        # flag and whether the source has audio:
        #
        #   * **concat demuxer** (default, or audio-less source) —
        #     stream-copies per-segment video + audio into one file.
        #     Fast, lossless for video, but preserves per-segment AAC
        #     priming (~21ms per segment at 48kHz) which accumulates as
        #     A/V drift on multi-segment outputs (10 segments → ~170ms).
        #
        #   * **concat filter** (``gapless_concat=True`` + audio source)
        #     — re-encodes through a single PCM pipeline so priming is
        #     added only once (not per-segment), giving gapless audio.
        #     Both video and audio are re-encoded (the concat filter's
        #     ``v=1:a=1`` joins both streams). The trade-off is one
        #     generation of video quality loss (H.264 → decode → H.264);
        #     for lossless video + gapless audio, use ``cut_then_encode``
        #     (one encode pass, but sacrifices frame accuracy via
        #     keyframe snap).
        #
        # Audio-less sources always use the demuxer path: there's no
        # priming to compensate for, so the concat filter would just
        # add a pointless re-encode of nothing.
        part_paths = [seg_dir / f"seg_{i:06d}.mp4" for i in range(n_segs)]

        # Windows CreateProcess caps the command line at 32,767 chars. The
        # gapless concat filter puts one ``-i <path>`` per segment PLUS the
        # whole ``[0:v][0:a]...[N:v][N:a]concat=n=N:v=1:a=1[outv][outa]``
        # graph inline, which blows past the limit with a few hundred
        # segments (measured: 381 segments → 48K chars → winerror 206).
        # In that case fall back to the concat demuxer, which references a
        # file list on disk and stays tiny regardless of segment count.
        # Estimate: each '-i "path"' ≈ path_len + 6; graph ≈ 17 chars per
        # segment plus template boilerplate.
        gapless_cmd_len_if_used = 0
        if gapless_concat and source_has_audio and n_segs > 1 and os.name == "nt":
            per_input = len(str(part_paths[0])) + 6 if part_paths else 0
            gapless_cmd_len_if_used = per_input * n_segs + n_segs * 17 + 400

        use_gapless = (
            gapless_concat
            and source_has_audio
            and n_segs > 1
            and (os.name != "nt" or gapless_cmd_len_if_used <= 30_000)
        )
        if gapless_concat and source_has_audio and n_segs > 1 and not use_gapless:
            logger.warning(
                f"gapless concat skipped: {n_segs} segments would exceed the "
                f"Windows 32K command-line limit (~{gapless_cmd_len_if_used} chars); "
                f"falling back to concat demuxer (stream copy)"
            )

        if use_gapless:
            _run_gapless_segment_concat(
                output_path,
                part_paths,
                vcodec,
                vcodec_opts,
                audio_quality=audio_quality,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )
        else:
            _run_final_concat(
                seg_dir,
                output_path,
                part_paths,
                total_duration=total_duration,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                label="segment concat",
                timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )
        logger.info(f"Successfully created output: {output_path}")

        # Cleanup on success
        shutil.rmtree(seg_dir, ignore_errors=True)

    except Exception:
        # On failure: keep segments for resume
        logger.info(f"Segments kept in {seg_dir} for resume on next run")
        raise


def _run_cut_then_encode(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: list[str],
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    encoder: str = "libx264",
    video_quality: str = "medium",
    audio_quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    source_has_audio: bool = True,
    output_fps: str = "source",
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    min_part_bytes: int = _MIN_PART_BYTES,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Cut lossless segments, concat losslessly, then do ONE final encode.

    Unlike ``_run_segment_concat`` (N encode passes + 1 lossless join)
    and ``_run_batch_concat`` (M chunk encodes + 1 lossless join), this
    method does:

      1. **Cut pass**: stream-copy each keep segment to a raw MKV
         (``-c copy`` -- no re-encode). Fast, lossless, low RAM.
      2. **Lossless concat**: join all raw segments into a single
         intermediate MKV via the concat demuxer (``-c copy``).
      3. **One final encode**: re-encode the intermediate with the
         chosen codec + quality. This is the *only* encode pass.

    The main win is **quality**: a single encode pass means no
    generation loss at segment boundaries, one continuous GOP, and one
    AAC audio encode (no per-segment priming drift). The trade-off is
    **disk space**: the raw intermediate can be ~1.5x the source size
    (the sum of keep segments' bytes at the source's original bitrate).

    Cut accuracy: ``-c copy`` snaps to the nearest preceding keyframe.
    For silence removal this is usually fine (cut points are in silent
    regions). Smart-cut (exact frame accuracy) is deferred to v2.

    Resume: same manifest + ffprobe validation as the other methods.
    Work dir: ``_<stem>_cut`` (distinct from ``_segments`` / ``_batch``
    so manifests don't collide).
    """
    n_segs = len(keep_segments)
    total_duration = sum(e - s for s, e in keep_segments)
    if total_duration <= 0:
        raise ConcatError("No video content to keep (total duration is zero)")

    cut_dir = output_path.parent / f"_{output_path.stem}_cut"
    raw_concat_path = cut_dir / "raw_concat.mkv"

    # Resume manifest (P0.6): same structure as the other methods so
    # a mismatch in source / encoder / keep_segments / pipeline_version
    # wipes the work dir.
    manifest = _build_manifest(
        video_path,
        keep_segments,
        "cut_then_encode",
        encoder,
        vcodec,
        vcodec_opts,
        video_quality,
        audio_quality,
        x264_preset,
        encoder_threads,
    )
    _ensure_fresh_work_dir(cut_dir, manifest)
    cut_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Phase 1: Cut pass (stream-copy each segment to MKV) ──
        # Progress: 0.0 .. 0.4 (cut is fast, so a small slice of the bar).
        cut_progress_base = 0.0
        cut_progress_span = 0.4

        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise CancelledError("cut_then_encode cancelled")
            dur = end - start
            cut_path = cut_dir / f"cut_{i:06d}.mkv"

            # Resume skip: if the file exists, is large enough, and
            # passes ffprobe validation AND has the expected duration,
            # reuse it.
            if (
                cut_path.exists()
                and cut_path.stat().st_size >= min_part_bytes
                and _ffprobe_is_valid_mp4(cut_path)
                and _ffprobe_duration_ok(cut_path, dur)
            ):
                logger.debug(f"cut_then_encode: reusing cut_{i:06d}.mkv")
                continue

            cmd = [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                "-ss",
                str(start),
                "-i",
                str(video_path),
                "-t",
                str(dur),
                "-map",
                "0:v:0",
            ]
            if source_has_audio:
                cmd.extend(["-map", "0:a:0?"])
            cmd.extend(
                [
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    str(cut_path),
                ]
            )
            _run_subprocess_cmd(
                cmd,
                timeout=segment_encode_timeout,
                label=f"cut_then_encode cut phase segment {i}",
                cancel_callback=cancel_callback,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
            )
            if progress_callback:
                progress_callback(cut_progress_base + (i + 1) / n_segs * cut_progress_span)

        # ── Phase 2: Lossless concat (concat demuxer → raw_concat.mkv) ──
        # Progress: 0.4 .. 0.5 (fast stream-copy join).
        part_paths = [cut_dir / f"cut_{i:06d}.mkv" for i in range(n_segs)]

        # Reuse _run_final_concat but point it at raw_concat.mkv instead
        # of the final output. The progress span for this step is small
        # (0.4..0.5) because it's a stream copy.
        if not raw_concat_path.exists() or not _ffprobe_is_valid_mp4(raw_concat_path):
            _run_final_concat(
                cut_dir,
                raw_concat_path,
                part_paths,
                total_duration=total_duration,
                progress_callback=(
                    (lambda f: progress_callback(0.4 + f * 0.1)) if progress_callback else None
                ),
                cancel_callback=cancel_callback,
                label="cut_then_encode concat",
                timeout=final_concat_timeout,
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
                memory_monitor_factory=memory_monitor_factory,
            )

        # ── Phase 3: One final encode ──
        # Progress: 0.5 .. 1.0 (this is the heavy step).
        encode_fps_filter: list[str] = []
        if output_fps != "source":
            encode_fps_filter = ["-vf", f"fps={output_fps}"]

        audio_encode_opts: list[str] = []
        if source_has_audio:
            audio_encode_opts = [
                "-map",
                "0:a:0?",
                "-c:a",
                "aac",
                *_audio_bitrate_opts(audio_quality),
                *_audio_opts(audio_quality),
            ]

        encode_progress_base = 0.5
        encode_progress_span = 0.5

        def _encode_prog(seconds: float) -> None:
            if progress_callback and total_duration > 0:
                frac = min(seconds / total_duration, 1.0)
                progress_callback(encode_progress_base + frac * encode_progress_span)

        label_text = "cut_then_encode final encode"
        _run_ffmpeg(
            [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(raw_concat_path),
                "-map",
                "0:v:0",
                *encode_fps_filter,
                "-c:v",
                vcodec,
                *vcodec_opts,
                *audio_encode_opts,
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            progress_callback=_encode_prog,
            timeout=final_concat_timeout,
            label=label_text,
            cancel_callback=cancel_callback,
            memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
            stall_kill=stall_kill,
            stall_warning=stall_warning,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
        )
        logger.info(f"Successfully created output (cut_then_encode): {output_path}")

        # Cleanup on success
        shutil.rmtree(cut_dir, ignore_errors=True)

    except Exception:
        logger.info(f"Cut intermediates kept in {cut_dir} for resume on next run")
        raise


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
    """Run segment or batch concat with the primary encoder; fall back to libx264 on failure.

    On encoder fallback the per-method working directory (``_<stem>_segments``
    or ``_<stem>_batch``) is wiped -- the chunks written by the failing
    encoder may be corrupt (e.g. h264_mf produces MP4s without a moov atom
    on some Windows builds) and the resume-skip check in the inner method
    would otherwise reuse them on the libx264 retry.

    ``method`` is "segment" or "batch"; anything else raises ConcatError.
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
        raise ConcatError(
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
            _run_segment_concat(
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
            _run_cut_then_encode(
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
            )
        else:
            _run_batch_concat(
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

    _with_libx264_fallback(
        primary_codec,
        primary_opts,
        _try,
        (ConcatError, OSError),
        _cleanup,
        video_quality=video_quality,
        audio_quality=audio_quality,
        software_fallback=software_fallback,
        fallback_consent=fallback_consent,
        x264_preset=x264_preset,
        encoder_threads=encoder_threads,
        x264_low_memory=x264_low_memory,
    )


def _run_batch_concat(
    video_path: Path,
    keep_segments: list[tuple[float, float]],
    output_path: Path,
    vcodec: str,
    vcodec_opts: list[str],
    progress_callback: Callable[[float], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    encoder: str = "libx264",
    video_quality: str = "medium",
    audio_quality: str = "medium",
    x264_preset: str = "medium",
    encoder_threads: str | int = "auto",
    source_has_audio: bool = True,
    output_fps: str = "source",
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT,
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT,
    stall_kill: int = _STALL_KILL,
    stall_warning: int = _STALL_WARNING,
    batch_chunk_size: int = _BATCH_CHUNK_SIZE,
    min_part_bytes: int = _MIN_PART_BYTES,
    low_process_priority: bool = False,
    rlimit_as_mb: int = 0,
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None,
) -> None:
    """Process chunks sequentially: each chunk → temp file, then concat.

    Previous approach built one giant filter graph with all chunks, causing
    ffmpeg to decode the entire video for every select/aselect filter in
    parallel -- O(chunks * filesize) RAM.  This version processes one chunk
    at a time so ffmpeg only holds ~1 chunk worth of decoded frames.

    Supports resume: already-encoded chunks are skipped on re-run. Resume
    integrity is enforced by the same manifest mechanism as the segment
    path (see ``_ensure_fresh_work_dir``); each chunk is also
    ffprobe-validated so a partial moov-atom crash artifact is detected
    and re-encoded.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_duration = sum(e - s for s, e in keep_segments)
    # Dynamic chunk size: scale down for large files to reduce per-chunk
    # RAM, scale up for small files to keep chunks productive.
    n_segs = len(keep_segments)
    if n_segs > 200:
        chunk_size = max(_BATCH_CHUNK_MIN, batch_chunk_size * 200 // n_segs)
    elif n_segs > 100 and total_duration > 3600:
        chunk_size = max(_BATCH_CHUNK_MIN, batch_chunk_size * 100 // n_segs)
    else:
        chunk_size = batch_chunk_size
    chunk_size = max(_BATCH_CHUNK_MIN, min(chunk_size, batch_chunk_size))
    chunks = [keep_segments[i : i + chunk_size] for i in range(0, n_segs, chunk_size)]
    n_chunks = len(chunks)
    logger.info(
        f"batch: {len(keep_segments)} segments in {n_chunks} chunks, "
        f"{total_duration:.1f}s output, {vcodec}"
    )

    batch_dir = output_path.parent / f"_{output_path.stem}_batch"
    manifest = _build_manifest(
        video_path,
        keep_segments,
        "batch",
        encoder,
        vcodec,
        vcodec_opts,
        video_quality,
        audio_quality,
        x264_preset,
        encoder_threads,
    )
    _ensure_fresh_work_dir(batch_dir, manifest)

    # PTS shift compensation (fix-plan §4: "Broken/non-zero timestamps").
    #
    # Sources captured with ``-itsoffset`` (OBS streams, mid-file
    # re-muxes) have a non-zero container ``start_time`` — the first
    # frame's PTS is shifted by a few seconds even though the per-frame
    # duration is unchanged. The batch path's ``-copyts`` preserves
    # these shifted PTS into the filter graph, so an absolute-time
    # ``trim={s}:{e}`` filter (where ``s``/``e`` are user-visible
    # source-time coordinates 0..N) would never match any frame on a
    # shifted source — every frame's PTS is ``start_time`` above the
    # trim window, so the chunk encodes 0 frames. Empirically verified:
    # a 6s testsrc source with ``-itsoffset 5.0`` and trim=2.0:4.0
    # produced 0 frames.
    #
    # The input-side ``-ss`` is interpreted by ffmpeg's MP4/MOV demuxer
    # in *file position* (source-time) terms, NOT in container-PST
    # terms — so a ``-ss 6.5`` on a source whose first frame has PTS=5
    # finds nothing (the demuxer thinks the file ends at duration+0
    # rather than duration+start_time). The two compensations therefore
    # move in opposite directions:
    #   * seek_to is shifted DOWN by ``start_time`` (file-position seek);
    #   * trim endpoints are shifted UP by ``start_time`` (PTS-space).
    # For a clean source (start_time=0) both shifts are zero, so the
    # historical behaviour is preserved exactly. Probed once before the
    # chunk loop so it doesn't add an ffprobe call per chunk.
    start_time = get_video_start_time(video_path)
    # Clamp negative start_time to 0. A negative container start_time
    # (e.g. -2.0 from DTS-based captures) means ffmpeg shifts timestamps
    # so the earliest DTS starts at 0 — the actual PTS timeline IS
    # 0-indexed, and compensating would shift the trim windows early by
    # |start_time|, cutting real content the user wants to keep.
    # ffmpeg's ``-avoid_negative_ts`` at the muxer level already zeroes
    # the DTS side; we just need to not double-compensate here.
    if start_time < 0.0:
        start_time = 0.0

    try:
        encoded_duration = 0.0
        skipped = 0

        for ci, chunk in enumerate(chunks):
            if cancel_callback and cancel_callback():
                raise CancelledError("batch encode cancelled")

            chunk_path = batch_dir / f"chunk_{ci:04d}.mp4"

            # Resume: skip already encoded chunks. Require both a minimum
            # size AND a successful ffprobe read so a crash artifact
            # (missing moov atom) doesn't get reused and produce a
            # corrupt chunk in the middle of the file.
            if (
                chunk_path.exists()
                and chunk_path.stat().st_size >= min_part_bytes
                and _ffprobe_is_valid_mp4(chunk_path)
                and _ffprobe_duration_ok(chunk_path, sum(e - s for s, e in chunk))
            ):
                skipped += 1
                encoded_duration += sum(e - s for s, e in chunk)
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_duration / total_duration, 0.9))
                continue

            chunk_start = chunk[0][0]
            chunk_end = chunk[-1][1]
            # P1.4: windowed decode. Previously each chunk read the
            # entire source from t=0 even though only [chunk_start,
            # chunk_end] was relevant -- on a 6h stream with 100 chunks
            # that's 600h of wasted decode. Coarse-seek ffmpeg to the
            # first keep segment's start with input-side ``-ss`` (fast
            # keyframe seek) and cap with ``-t`` so the demuxer stops
            # reading once we're past the chunk's last keep segment.
            # ``-copyts`` preserves source PTS so the ``trim`` filters
            # below still match the original timestamps (the seek just
            # skips keyframes; ffmpeg rewrites PTS to 0 without
            # ``-copyts`` and our absolute-time ``trim`` filters would
            # never match). A small keyframe-safety margin is added so
            # the seek doesn't drop a frame at the chunk's left edge.
            #
            # PTS compensation: on sources with a non-zero
            # ``start_time`` the seek value is decremented (see the
            # long comment above ``get_video_start_time``) while the
            # ``trim`` endpoints below are incremented by the same
            # amount. Both compensations are no-ops when start_time=0.
            _CHUNK_SEEK_MARGIN = 0.5
            seek_to = max(0.0, chunk_start - _CHUNK_SEEK_MARGIN - start_time)
            chunk_dur = chunk_end - seek_to

            # Frame-accurate, gapless chunk filter -- ``trim`` per keep
            # segment + ``concat`` filter glue.
            #
            # The earlier pipeline used a single ``select='between(...)+...``
            # over the whole chunk followed by ``setpts=N/FRAME_RATE/TB``.
            # Two problems with that formulation:
            #   1. ``FRAME_RATE`` is the source's nominal frame-rate
            #      constant; on VFR sources (and even some CFR ones --
            #      verified on a 30 FPS testsrc input) it disagrees
            #      with actual cadence and comes out as 25 FPS,
            #      dropping ~18-31 frames per 6s.
            #   2. ``setpts=PTS-STARTPTS`` (a tempting alternative that
            #      also keeps "real" timestamps) does NOT close the gap
            #      in PTS created by ``select`` -- the second kept range
            #      still carries its original absolute PTS (3.0..5.0)
            #      after subtracting the first kept frame's PTS (0),
            #      so container duration reports 5.03s even though only
            #      122 frames were emitted. The result is a VFR-style
            #      timeline where the player sees a 1-second freeze.
            #
            # The fix uses the ``concat`` filter on ``trim``-ed pieces --
            # the explicit concat operation is what actually closes the
            # gap and renumbers PTS so the chunk is gapless CFR. This
            # mirrors the segment path's "encode each piece, concat
            # demuxer" philosophy but inside a single ffmpeg invocation.
            #
            # Verified on a 6s/30FPS testsrc source with keep=[(0,2),(3,5)]:
            # the trim+concat graph produces duration=4.000s, frames=120,
            # r_frame_rate=30/1 -- frame-exact. ``select``+``setpts=N/FR/TB``
            # produced 4.07s/122 frames (acceptable); the previous
            # ``setpts=PTS-STARTPTS`` produced 5.03s/122 (BROKEN).
            #
            # Each kept range maps to two filter chains (v + a) and one
            # concat call at the end glues them. ``concat=n=N:v=1:a=1``
            # in filter form renumbers PTS internally so no manual
            # ``setpts`` is needed after the final concat.
            v_chains = []
            a_chains = []
            fps_suffix = _fps_filter_chain(output_fps)
            for idx, (s, e) in enumerate(chunk):
                # ``s``/``e`` are absolute source timestamps (user-visible
                # 0..N). The seek above made ffmpeg start at ``seek_to``
                # in file-position terms; with ``-copyts`` the PTS in the
                # filter graph are still the input's *container* PTS,
                # which on a shifted source starts at ``start_time`` and
                # runs to ``start_time + duration``. The trim filter
                # matches PTS values, so its endpoints must be moved up
                # by ``start_time`` to land in the right place on the
                # shifted PTS timeline. For start_time=0 this is
                # identical to the historical absolute-source-time path.
                #
                # P1.17: when ``output_fps != "source"``, splice an
                # ``fps=<target>`` filter AFTER ``setpts=PTS-STARTPTS``
                # so the new PTS cadence is the source's, not the
                # synthetic ``N/FRAME_RATE`` one. ``fps`` duplicates or
                # drops frames to match the CFR target.
                v_chains.append(
                    f"[0:v]trim={s + start_time}:{e + start_time},"
                    f"setpts=PTS-STARTPTS{fps_suffix}[v{idx}]"
                )
                # Audio chain is only built when the source actually has
                # an audio stream -- otherwise ``[0:a]atrim=...`` would
                # reference a non-existent input pad and ffmpeg would
                # fail mid-graph. The concat filter's ``a=1`` flag is
                # similarly dropped for audio-less sources so the output
                # is video-only. See P1.14 in the fix plan.
                #
                # ``apad`` + ``atrim=0:duration`` pads/trims the audio
                # chain to exactly match the video chain's duration so
                # the concat filter doesn't see a shorter audio stream
                # bleeding silence into the next segment's timeslot
                # (audio-outlives-video inside the final concat demuxer
                # join). The padding is silence, so no audible artifact.
                if source_has_audio:
                    a_chains.append(
                        f"[0:a]atrim={s + start_time}:{e + start_time},asetpts=PTS-STARTPTS,"
                        f"apad,atrim=0:{e - s}[a{idx}]"
                    )
            n = len(chunk)
            if source_has_audio:
                concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
                graph = (
                    ";".join(v_chains + a_chains)
                    + f";{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"
                )
            else:
                concat_inputs = "".join(f"[v{i}]" for i in range(n))
                graph = ";".join(v_chains) + f";{concat_inputs}concat=n={n}:v=1:a=0[outv]"

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(graph)
                f.flush()
                os.fsync(f.fileno())
                script_path = f.name

            try:

                def _chunk_prog(
                    seconds: float, _chunk: Any = chunk, _encoded_duration: float = encoded_duration
                ) -> None:
                    chunk_dur = sum(e - s for s, e in _chunk)
                    if progress_callback and total_duration > 0:
                        base = _encoded_duration / total_duration
                        span = chunk_dur / total_duration
                        # ffmpeg's `out_time_us` reflects *output* time
                        # (the `select` filter skips silence patterns),
                        # so dividing it by `total_duration` overruns the
                        # `base + span` ceiling well before the chunk
                        # finishes. Convert to a per-chunk fraction first,
                        # then scale to absolute progress -- same trick as
                        # `_seg_prog` in `_run_segment_concat`.
                        frac = min(seconds / chunk_dur, 1.0) if chunk_dur > 0 else 1.0
                        progress_callback(min(base + frac * span, 0.9))

                label_text = f"batch chunk {ci}/{n_chunks}"
                _run_ffmpeg(
                    [
                        ffmpeg_path(),
                        "-y",
                        "-loglevel",
                        "error",
                        "-progress",
                        "pipe:1",
                        # P1.4: windowed decode. ``-ss`` before ``-i``
                        # fast-seeks to chunk_start; ``-copyts`` keeps
                        # source PTS so the absolute-time ``trim=...``
                        # filters below still match. ``-t`` caps the
                        # demuxer so we don't decode the whole source.
                        "-ss",
                        f"{seek_to:.3f}",
                        "-copyts",
                        "-i",
                        str(video_path),
                        "-t",
                        f"{chunk_dur:.3f}",
                        "-filter_complex_script",
                        script_path,
                        "-map",
                        "[outv]",
                        "-c:v",
                        vcodec,
                        *vcodec_opts,
                        # Audio mapping only when the source has audio
                        # (and the graph therefore produced [outa]).
                        # Without this guard a video-only source would
                        # fail with "Stream map '[outa]' matches no
                        # stream" -- see P1.14.
                        *(
                            [
                                "-map",
                                "[outa]",
                                "-c:a",
                                "aac",
                                *_audio_bitrate_opts(audio_quality),
                                *_audio_opts(audio_quality),
                            ]
                            if source_has_audio
                            else []
                        ),
                        str(chunk_path),
                    ],
                    progress_callback=_chunk_prog,
                    timeout=segment_encode_timeout,
                    label=label_text,
                    cancel_callback=cancel_callback,
                    memory_monitor=_new_memory_monitor(memory_monitor_factory, label_text),
                    stall_kill=stall_kill,
                    stall_warning=stall_warning,
                    low_process_priority=low_process_priority,
                    rlimit_as_mb=rlimit_as_mb,
                )
            finally:
                Path(script_path).unlink(missing_ok=True)

            encoded_duration += sum(e - s for s, e in chunk)
            logger.info(
                f"batch chunk {ci + 1}/{n_chunks} done ({chunk_path.stat().st_size // 1024 // 1024} MB)"
            )

        if skipped:
            logger.info(
                f"batch: resumed {skipped}/{n_chunks} already encoded, encoded {n_chunks - skipped}"
            )

        # Final concat demuxer pass -- shared with _run_segment_concat (P2.6).
        part_paths = [batch_dir / f"chunk_{ci:04d}.mp4" for ci in range(n_chunks)]
        _run_final_concat(
            batch_dir,
            output_path,
            part_paths,
            total_duration=total_duration,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            label="batch concat",
            timeout=final_concat_timeout,
            stall_kill=stall_kill,
            stall_warning=stall_warning,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
            memory_monitor_factory=memory_monitor_factory,
        )
        logger.info(f"batch: successfully created {output_path}")

        # Cleanup on success
        shutil.rmtree(batch_dir, ignore_errors=True)

    except Exception:
        # On failure: keep chunks for resume
        logger.info(f"Chunks kept in {batch_dir} for resume on next run")
        raise


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
    """
    enc, enc_opts = primary_codec, primary_opts
    while True:
        try:
            try_fn(enc, enc_opts)
            return
        except CancelledError:
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
                encoder_opts(
                    "libx264",
                    video_quality,
                    x264_preset=x264_preset,
                    encoder_threads=encoder_threads,
                    x264_low_memory=x264_low_memory,
                ),
            )
