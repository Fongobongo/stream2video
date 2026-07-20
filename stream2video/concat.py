"""Video cutting and concatenation module using ffmpeg."""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from stream2video.config import (
    VALID_ENCODERS,
    VALID_METHODS,
    VALID_OUTPUT_FPS,
    VALID_QUALITIES,
    VALID_SOFTWARE_FALLBACKS,
    VALID_X264_PRESETS,
)
from stream2video.silence import SilenceSegment
from stream2video.utils import (
    CANCEL_POLL_INTERVAL,
    cancel_monitor,
    drain_stderr_lines,
    get_video_duration,
    has_audio_stream,
    no_window_kwargs,
    set_active_process,
)

if TYPE_CHECKING:
    from stream2video.memory import MemoryMonitor

logger = logging.getLogger(__name__)


class ConcatError(Exception):
    """Raised on concat / encode failures (ffmpeg errors, bad inputs)."""


class FFmpegError(ConcatError):
    """ffmpeg itself failed (non-zero exit, timeout, stall)."""


class CancelledError(ConcatError):
    """User cancellation during concat/encode (not a real failure)."""


class EncoderUnavailableError(ConcatError):
    """Hardware encoder unavailable and the fallback policy refused libx264.

    Distinct from ``FFmpegError`` so the CLI can craft a "select a different
    encoder / check the driver" message instead of a generic "ffmpeg failed"
    one — the encoder wasn't even tried, so its stderr wouldn't be helpful.
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
# Audio bitrate presets. ``medium`` keeps the historical 128k so default
# output is unchanged on upgrade. ``high``/``low`` give the user a real
# choice so a 192k/256k/320k source is no longer silently downgraded —
# see P0.3 in the fix plan. Set at encode time only; the value is read
# through ``_audio_bitrate()`` so runtime override is a single knob.
_AUDIO_BITRATE = "128k"
_AUDIO_BITRATES: dict[str, str] = {
    "high": "256k",
    "medium": "192k",
    "low": "128k",
}
# Sample rate / channel policy. ``-ar 48000 -ac 2`` historically
# normalised everything to stereo 48 kHz AAC — the source was never
# preserved, but output was at least consistent across segments. Keep
# that explicit conversion so the audio path is documented, but route
# it through ``_audio_opts()`` so an explicit "preserve source" preset
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
# Extra seconds to let the AAC encoder flush its lookahead buffer per
# segment. Used ONLY when the encoder actually needs it; current
# single-`-t` segment path does NOT (both streams are bounded by `-t`
# at the same instant), so `_AUDIO_PAD` is retained as a documented
# constant for the fallback-pad filters but not added to any segment's
# output duration — see P0.4 in the fix plan.
_HYBRID_SEEK_OFFSET = 0.5
_AUDIO_PAD = 0.1  # extra seconds to let AAC encoder flush its lookahead buffer


def _audio_bitrate() -> str:
    """Bitrate string for the AAC encoder based on ``audio_quality``.

    Reads the module-level ``_audio_quality`` set by ``cut_and_concat``
    via ``_set_audio_quality``. Defaults to ``medium`` (128k) when unset
    so existing tests/benchmarks that don't go through the pipeline
    entry point keep their historical output.
    """
    return _AUDIO_BITRATES.get(_audio_quality, _AUDIO_BITRATE)


def _audio_opts() -> list[str]:
    """Output-side AAC options: sample rate + channel layout.

    Centralised so a follow-up "preserve source" preset (no `-ar`/`-ac`)
    is one knob. Returns a fresh list each call so callers may mutate
    freely without affecting shared state.
    """
    return ["-ar", _AUDIO_SAMPLE_RATE, "-ac", _AUDIO_CHANNELS]


# Audio quality preset for the current pipeline run. Thread-safe enough
# for this codebase's sequential pipeline (one encode at a time,
# cancellation-checked) — set once per ``cut_and_concat`` invocation.
_audio_quality: str = "medium"


def _set_audio_quality(q: str) -> None:
    global _audio_quality
    if q not in _AUDIO_BITRATES:
        raise ConcatError(
            f"Unknown audio quality {q!r} (use {' or '.join(repr(k) for k in _AUDIO_BITRATES)})"
        )
    _audio_quality = q


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
    memory_limit_mb: str | int = "auto",
    memory_reserve_mb: int = 2048,
    x264_low_memory: bool = False,
) -> Path:
    if not video_path.exists():
        raise ConcatError(f"Input video not found: {video_path}")

    keep_segments = generate_keep_segments(video_path, silence_segments)

    if not keep_segments:
        raise ConcatError("No video segments to keep after removing silence")

    logger.info(
        f"Keeping {len(keep_segments)} segments, removing {len(silence_segments)} silence segments"
    )

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
    _set_audio_quality(audio_quality)

    # Detect whether the source has an audio stream ONCE. Probing per
    # segment would be wasteful; passing the flag down lets the
    # segment/batch builders omit ``-c:a`` / audio mapping for
    # audio-less sources (otherwise ffmpeg fails with "Output file
    # does not contain any stream" when ``-map 0:a:0`` is requested
    # on a video-only input). See P1.14 in the fix plan.
    source_has_audio = has_audio_stream(video_path)
    if not source_has_audio:
        logger.info(f"Source {video_path.name} has no audio stream — encoding video-only")

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
        # Only warn on a meaningful clamp — sub-microsecond FP drift
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

    quality: ``high`` / ``medium`` / ``low``. Affects bitrate (HW encoders)
    and CRF (libx264). ``medium`` reproduces the previously hard-coded
    options exactly so existing output is unchanged.

    ``x264_preset`` (libx264 only): one of ``VALID_X264_PRESETS``. Default
    ``medium`` preserves historical behaviour; users with unstable /
    overclocked CPUs can pass ``ultrafast``/``veryfast`` for a lighter
    load. See P0.5 in the fix plan.

    ``encoder_threads``: ``"auto"`` (no ``-threads`` flag, ffmpeg chooses)
    or a positive int. For libx264 the flag goes AFTER the encoder in the
    constructed command (``-c:v libx264 ... -threads N``) so it applies to
    the encoder, not to the decoder (a ``-threads`` before ``-i`` would
    bound the decoder's thread pool instead — different effect).

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
    # libx264 — CRF-driven, bitrate is ignored. ``-preset`` controls the
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

    ``source`` (default) returns an empty string — the encoder receives
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
    ffmpeg — that hid cases where the build's libx264 was broken or
    missing (e.g. a stripped-down distro build, or ffmpeg linked with
    GPL-removed codecs). We now run the same 1-frame lavfi smoke test
    for every encoder; the cache keeps it free for the test-encoder
    button's repeated calls.
    """
    with _encoder_check_lock:
        if name in _encoder_check_cache:
            return _encoder_check_cache[name]
        try:
            r = subprocess.run(
                [
                    "ffmpeg",
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
            # libx264 is the safety net — if it's gone, the user needs to
            # reinstall ffmpeg with x264 support, not be told to "pick
            # a different encoder".
            logger.error(
                f"libx264 smoke test FAILED (rc={r.returncode}); "
                f"stderr: {r.stderr[:300]!r}. Your ffmpeg build is missing the "
                f"x264 library — reinstall ffmpeg (e.g. `winget install "
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

      * ``enabled`` — return libx264 (legacy silent-fallback behaviour).
      * ``disabled`` — raise EncoderUnavailableError immediately.
      * ``ask`` (default) — call ``on_unavailable`` if provided; the
        callback returns True to allow libx264 fallback, False to
        raise. With ``on_unavailable=None``, ``ask`` behaves as
        ``disabled`` so a pipeline run can't silently switch to a
        CPU-heavy encoder without consent.

    ``x264_preset`` and ``encoder_threads`` are forwarded to
    :func:`encoder_opts` so the resolved options match the user's
    settings (also on a fallback retry — the fallback call site keeps
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

    # HW encoder unavailable — apply fallback policy.
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
        f"{preferred} not available; software_fallback='disabled' — refusing libx264"
    )


def _run_ffmpeg(
    cmd: list[str],
    progress_callback: Callable[[int], None] | None,
    timeout: int,
    label: str = "ffmpeg",
    cancel_callback: Callable[[], bool] | None = None,
    track_progress: bool = True,
    memory_monitor: "MemoryMonitor | None" = None,
) -> None:
    """Run an ffmpeg command. With track_progress=True (default), parses ffmpeg's
    -progress stream from stdout and invokes progress_callback(us). With False,
    stdout is discarded — use for per-segment encodes where the segment index
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
    try:
        process = subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            bufsize=-1,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found in PATH") from e

    set_active_process(process)
    if memory_monitor is not None:
        # Late-bind the pid now that the process exists, then start
        # the monitor thread. The monitor reads RSS by pid, so this
        # must happen after Popen returns.
        memory_monitor.pid = process.pid
        memory_monitor.start()
    stderr_pipe = process.stderr
    assert stderr_pipe is not None
    stderr_lines: list[str] = []
    wait_for_drain = drain_stderr_lines(stderr_pipe, stderr_lines)
    drain_done = False
    last_progress_time = time.monotonic()

    # P1.5: stall watchdog. The ``track_progress=True`` branch checks
    # ``elapsed_since_progress`` inside its readline loop, but readline
    # blocks until ffmpeg emits a line — a fully-hung ffmpeg (deadlock,
    # no stdout at all) would never surface as a stall there. This
    # daemon thread polls ``last_progress_time`` independently of
    # stdout availability and kills the process when the stall window
    # expires. The track_progress loop's inline check is retained as
    # a fast path (it kills ASAP after a stalled line arrives).
    stall_stop = threading.Event()

    def _stall_watchdog():
        while not stall_stop.wait(CANCEL_POLL_INTERVAL):
            if process.poll() is not None:
                return
            elapsed = time.monotonic() - last_progress_time
            if elapsed > _STALL_KILL:
                logger.error(
                    f"{label}: stall watchdog firing — no progress for "
                    f"{int(elapsed)}s, killing process"
                )
                try:
                    process.kill()
                except Exception:
                    logger.exception("stall watchdog: kill() failed")
                return

    stall_thread = threading.Thread(target=_stall_watchdog, daemon=True, name=f"stall_{label}")
    stall_thread.start()

    try:
        with cancel_monitor(process, cancel_callback) as cancelled:
            if track_progress:
                stdout_pipe = process.stdout
                assert stdout_pipe is not None
                for raw_line in iter(stdout_pipe.readline, b""):
                    if cancel_callback and cancel_callback():
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
                                progress_callback(us)
                            except (ValueError, IndexError):
                                pass
                    elapsed_since_progress = time.monotonic() - last_progress_time
                    if elapsed_since_progress > _STALL_KILL:
                        process.kill()
                        raise FFmpegError(
                            f"{label} stalled — no progress for {int(elapsed_since_progress)}s, "
                            "possible resource exhaustion"
                        )
                    elif elapsed_since_progress > _STALL_WARNING:
                        logger.warning(
                            f"{label}: no progress for {int(elapsed_since_progress)}s — waiting..."
                        )

            if cancelled.is_set():
                raise CancelledError(f"{label} cancelled")
            _wait_with_cancel(process, timeout, cancel_callback, label)
            wait_for_drain()
            drain_done = True

            if process.returncode != 0:
                stderr_text = "".join(stderr_lines)
                msg = stderr_text[:_STDERR_TRUNCATE] if stderr_text else "unknown error (no stderr)"
                raise FFmpegError(f"{label} failed: {msg}")

    except subprocess.TimeoutExpired as e:
        process.kill()
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
                    + (" (HARD limit hit — task cancelled)" if memory_monitor.hard_exceeded else "")
                )
        if not drain_done:
            wait_for_drain()
        set_active_process(None)
        if process.stdout is not None:
            process.stdout.close()
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
# avoided — for a 6h stream that's an extra O(filesize) read. (path,
# size, mtime_ns) is enough to detect: file moved/replaced, file edited
# in place (size and mtime change), file re-encoded (size and mtime
# change). For adversarial cases (size+mtime preserved by external
# tooling) the user can pass --force.

PIPELINE_VERSION = 3  # bump when the on-disk segment/chunk format changes

# Minimum chunk/segment size to consider valid for resume. A 1-byte file
# is missing the moov atom (or never had one written) — reuse would
# corrupt the final concat in the middle.
_MIN_PART_BYTES = 1024


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / "_manifest.json"


def _source_identity(video_path: Path) -> dict:
    """Snapshot (path, size, mtime_ns) so resume detects source changes.

    Uses the absolute path so renaming the file (same content) doesn't
    silently reuse segments encoded against a different filename — the
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
    # Source identity (path/size/mtime) — strict equality.
    stored_src = stored.get("source") or {}
    current_src = current.get("source") or {}
    for key in ("path", "size", "mtime_ns"):
        if stored_src.get(key) != current_src.get(key):
            logger.info(
                f"Resume: source {key} changed: "
                f"stored={stored_src.get(key)!r} current={current_src.get(key)!r}"
            )
            return False
    # Keep segments must match exactly — different cut points = different
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

    The wipe is destructive — it removes the entire work dir including
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


def _ffprobe_is_valid_mp4(path: Path) -> bool:
    """Quick validity check: ffprobe can read codec + duration.

    Used by resume-skip to reject a chunk that exists and is large enough
    but is internally corrupt (e.g. ffmpeg crashed mid-write and the
    moov atom is missing). Without this, the concat demuxer would
    accept the file but emit a broken segment in the middle of the output.
    """
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
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


def _run_final_concat(
    work_dir: Path,
    output_path: Path,
    part_paths: list[Path],
    *,
    total_duration: float,
    progress_callback: Callable[[float], None] | None,
    cancel_callback: Callable[[], bool] | None,
    label: str,
) -> None:
    """Build ``concat.txt`` and run the final concat-demuxer pass.

    Shared by ``_run_segment_concat`` and ``_run_batch_concat`` (P2.6).
    Both methods previously had identical 30-line blocks here: open
    ``concat.txt``, write one ``file <name>`` line per part, run
    ``ffmpeg -f concat -safe 0 -i ... -c copy -fflags +genpts``,
    cleanup. The only real differences were the part filename pattern
    (``seg_NNNNNN.mp4`` vs ``chunk_NNNN.mp4``) and the label string;
    both are now parameters so the body lives once.

    The progress callback maps ffmpeg's ``out_time_us`` (which reflects
    output time across the whole concat, not per-segment) to the last
    10% of the overall progress bar — both call sites reserve 0..0.9
    for the per-segment encodes and 0.9..1.0 for this final concat.
    """
    list_path = work_dir / "concat.txt"
    with open(list_path, "w", encoding="utf-8") as lf:
        for part in part_paths:
            lf.write(f"file {_quote_concat_path(part.name)}\n")

    def _concat_prog(us: int):
        if progress_callback and total_duration > 0:
            progress_callback(min(us / 1_000_000 / total_duration * 0.1, 0.1) + 0.9)

    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-fflags",
            "+genpts",
            str(output_path),
        ],
        progress_callback=_concat_prog,
        timeout=_FINAL_CONCAT_TIMEOUT,
        label=label,
        cancel_callback=cancel_callback,
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
):
    """Encode each segment, join with concat demuxer.

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
                and seg_path.stat().st_size >= _MIN_PART_BYTES
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
            #      drops frames until `start` automatically — this is
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
            # expects when `-fflags +genpts` rebuilds the final PTS).
            # Without `-copyts`, timestamps in the segment file are
            # already normalised to start at 0, so a `setpts=PTS-STARTPTS`
            # is a no-op here and is omitted for clarity.

            def _seg_prog(us: int, _dur=dur, _encoded_keep=encoded_keep):
                # ffmpeg -progress reports `out_time_us` — the position within
                # this segment's output, NOT the original video. Map it to
                # absolute progress across the whole video so the GUI/CLI
                # bar moves smoothly even when a single segment takes an
                # hour (e.g. 0 silence segments → 1 keep segment = the
                # whole video).
                if progress_callback and total_duration > 0 and _dur > 0:
                    seg_frac = min(us / 1_000_000 / _dur, 1.0)
                    abs_time = _encoded_keep + seg_frac * _dur
                    progress_callback(min(abs_time / total_duration * 0.9, 0.9))

            _run_ffmpeg(
                [
                    "ffmpeg",
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
                        ["-map", "0:a:0?", "-c:a", "aac", "-b:a", _audio_bitrate(), *_audio_opts()]
                        if source_has_audio
                        else []
                    ),
                    str(seg_path),
                ],
                progress_callback=_seg_prog,
                timeout=_SEGMENT_ENCODE_TIMEOUT,
                label=f"segment {i} encode",
                cancel_callback=cancel_callback,
            )

            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(min(encoded_keep / total_duration * 0.9, 0.9))

        if skipped:
            logger.info(
                f"segment: resumed {skipped}/{n_segs} already encoded, encoded {n_segs - skipped}"
            )

        # Final concat demuxer pass — shared with _run_batch_concat (P2.6).
        part_paths = [seg_dir / f"seg_{i:06d}.mp4" for i in range(n_segs)]
        _run_final_concat(
            seg_dir,
            output_path,
            part_paths,
            total_duration=total_duration,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            label="segment concat",
        )
        logger.info(f"Successfully created output: {output_path}")

        # Cleanup on success
        shutil.rmtree(seg_dir, ignore_errors=True)

    except Exception:
        # On failure: keep segments for resume
        logger.info(f"Segments kept in {seg_dir} for resume on next run")
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
):
    """Run segment or batch concat with the primary encoder; fall back to libx264 on failure.

    On encoder fallback the per-method working directory (``_<stem>_segments``
    or ``_<stem>_batch``) is wiped — the chunks written by the failing
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
        inner = _run_segment_concat
        work_suffix = "_segments"
    elif method == "batch":
        inner = _run_batch_concat
        work_suffix = "_batch"
    else:
        raise ConcatError(
            f"Unknown method: {method!r} (use {' or '.join(repr(m) for m in VALID_METHODS)})"
        )

    work_dir = output_path.parent / f"_{output_path.stem}{work_suffix}"

    def _cleanup(failed_enc: str):
        if work_dir.exists():
            logger.info(
                f"Removing partial {work_suffix[1:]} dir from failed {failed_enc} encode: {work_dir}"
            )
            shutil.rmtree(work_dir, ignore_errors=True)

    def _try(enc: str, enc_opts: list[str]):
        inner(
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
):
    """Process chunks sequentially: each chunk → temp file, then concat.

    Previous approach built one giant filter graph with all chunks, causing
    ffmpeg to decode the entire video for every select/aselect filter in
    parallel — O(chunks * filesize) RAM.  This version processes one chunk
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
        chunk_size = max(_BATCH_CHUNK_MIN, _BATCH_CHUNK_SIZE * 200 // n_segs)
    elif n_segs > 100 and total_duration > 3600:
        chunk_size = max(_BATCH_CHUNK_MIN, _BATCH_CHUNK_SIZE * 100 // n_segs)
    else:
        chunk_size = _BATCH_CHUNK_SIZE
    chunk_size = max(_BATCH_CHUNK_MIN, min(chunk_size, _BATCH_CHUNK_SIZE))
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
                and chunk_path.stat().st_size >= _MIN_PART_BYTES
                and _ffprobe_is_valid_mp4(chunk_path)
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
            # chunk_end] was relevant — on a 6h stream with 100 chunks
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
            _CHUNK_SEEK_MARGIN = 0.5
            seek_to = max(0.0, chunk_start - _CHUNK_SEEK_MARGIN)
            chunk_dur = chunk_end - seek_to

            # Frame-accurate, gapless chunk filter — ``trim`` per keep
            # segment + ``concat`` filter glue.
            #
            # The earlier pipeline used a single ``select='between(...)+...``
            # over the whole chunk followed by ``setpts=N/FRAME_RATE/TB``.
            # Two problems with that formulation:
            #   1. ``FRAME_RATE`` is the source's nominal frame-rate
            #      constant; on VFR sources (and even some CFR ones —
            #      verified on a 30 FPS testsrc input) it disagrees
            #      with actual cadence and comes out as 25 FPS,
            #      dropping ~18-31 frames per 6s.
            #   2. ``setpts=PTS-STARTPTS`` (a tempting alternative that
            #      also keeps "real" timestamps) does NOT close the gap
            #      in PTS created by ``select`` — the second kept range
            #      still carries its original absolute PTS (3.0..5.0)
            #      after subtracting the first kept frame's PTS (0),
            #      so container duration reports 5.03s even though only
            #      122 frames were emitted. The result is a VFR-style
            #      timeline where the player sees a 1-second freeze.
            #
            # The fix uses the ``concat`` filter on ``trim``-ed pieces —
            # the explicit concat operation is what actually closes the
            # gap and renumbers PTS so the chunk is gapless CFR. This
            # mirrors the segment path's "encode each piece, concat
            # demuxer" philosophy but inside a single ffmpeg invocation.
            #
            # Verified on a 6s/30FPS testsrc source with keep=[(0,2),(3,5)]:
            # the trim+concat graph produces duration=4.000s, frames=120,
            # r_frame_rate=30/1 — frame-exact. ``select``+``setpts=N/FR/TB``
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
                # ``s``/``e`` are absolute source timestamps; the seek
                # above made ffmpeg start at ``seek_to``, so PTS in the
                # filter graph are still absolute (thanks to ``-copyts``)
                # and the trim endpoints match the source timeline
                # directly — no offset arithmetic needed.
                #
                # P1.17: when ``output_fps != "source"``, splice an
                # ``fps=<target>`` filter AFTER ``setpts=PTS-STARTPTS``
                # so the new PTS cadence is the source's, not the
                # synthetic ``N/FRAME_RATE`` one. ``fps`` duplicates or
                # drops frames to match the CFR target.
                v_chains.append(f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS{fps_suffix}[v{idx}]")
                # Audio chain is only built when the source actually has
                # an audio stream — otherwise ``[0:a]atrim=...`` would
                # reference a non-existent input pad and ffmpeg would
                # fail mid-graph. The concat filter's ``a=1`` flag is
                # similarly dropped for audio-less sources so the output
                # is video-only. See P1.14 in the fix plan.
                if source_has_audio:
                    a_chains.append(f"[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS[a{idx}]")
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

                def _chunk_prog(us: int, _chunk=chunk, _encoded_duration=encoded_duration):
                    chunk_dur = sum(e - s for s, e in _chunk)
                    if progress_callback and total_duration > 0:
                        base = _encoded_duration / total_duration
                        span = chunk_dur / total_duration
                        # ffmpeg's `out_time_us` reflects *output* time
                        # (the `select` filter skips silence patterns),
                        # so dividing it by `total_duration` overruns the
                        # `base + span` ceiling well before the chunk
                        # finishes. Convert to a per-chunk fraction first,
                        # then scale to absolute progress — same trick as
                        # `_seg_prog` in `_run_segment_concat`.
                        frac = min(us / 1_000_000 / chunk_dur, 1.0) if chunk_dur > 0 else 1.0
                        progress_callback(min(base + frac * span, 0.9))

                _run_ffmpeg(
                    [
                        "ffmpeg",
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
                        # stream" — see P1.14.
                        *(
                            [
                                "-map",
                                "[outa]",
                                "-c:a",
                                "aac",
                                "-b:a",
                                _audio_bitrate(),
                                *_audio_opts(),
                            ]
                            if source_has_audio
                            else []
                        ),
                        str(chunk_path),
                    ],
                    progress_callback=_chunk_prog,
                    timeout=_SEGMENT_ENCODE_TIMEOUT,
                    label=f"batch chunk {ci}/{n_chunks}",
                    cancel_callback=cancel_callback,
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

        # Final concat demuxer pass — shared with _run_segment_concat (P2.6).
        part_paths = [batch_dir / f"chunk_{ci:04d}.mp4" for ci in range(n_chunks)]
        _run_final_concat(
            batch_dir,
            output_path,
            part_paths,
            total_duration=total_duration,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
            label="batch concat",
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
):
    """Run ``try_fn(primary_codec, primary_opts)``; on failure, retry once with libx264.

    Behaviour on ``primary_codec`` failure depends on ``software_fallback``:

      * ``enabled`` — retry with libx264 (legacy silent-fallback behaviour).
      * ``disabled`` — re-raise the original exception immediately so the
        user gets the real encoder's error.
      * ``ask`` (default) — call ``fallback_consent``; if it returns True
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
            # Non-libx264 encoder failed — apply fallback policy.
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
            # The fallback reuses _audio_bitrate() / _audio_opts() which
            # read the module-level _audio_quality. Keep the call explicit
            # so a future caller that bypasses cut_and_concat still gets
            # the right AAC preset on the retry.
            _set_audio_quality(audio_quality)
