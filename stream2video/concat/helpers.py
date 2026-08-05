"""Small pure helpers shared by the concat pipeline."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from stream2video.concat.constants import (
    _AUDIO_BITRATE,
    _AUDIO_BITRATES,
    _AUDIO_CHANNELS,
    _AUDIO_SAMPLE_RATE,
)
from stream2video.concat.errors import ConcatError
from stream2video.config import VALID_QUALITIES
from stream2video.memory import MemoryMonitor, auto_budget_mb

if TYPE_CHECKING:
    from stream2video.silence import SilenceSegment

logger = logging.getLogger(__name__)


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


def generate_keep_segments(
    video_path: Path,
    silence_segments: "list[SilenceSegment]",
) -> list[tuple[float, float]]:
    # ``get_video_duration`` is resolved through the package so a test
    # patching ``stream2video.concat.get_video_duration`` intercepts.
    from stream2video import concat as _c

    duration = _c.get_video_duration(video_path)
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
