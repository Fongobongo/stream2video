"""Public API for the silence-detection package.

This package replaces the historical ``silence.py`` module (1248
lines). ``stream2video.silence.<name>`` keeps working as before — the
submodules are an internal decomposition detail.

Layout::

    stream2video/silence/parser.py    -- SilenceSegment, SilenceParser, apply_margin
    stream2video/silence/cache.py     -- silence-cache read/write + resume cache
    stream2video/silence/detect.py    -- ffmpeg silencedetect driver + WAV extract
    stream2video/silence/pipeline.py  -- detect_silence orchestrator
"""

# Indirection layer: tests patch ``stream2video.silence.<name>`` for
# the timeout probes / ffmpeg helpers; keep them as attributes of the
# package so the patch succeeds. ``subprocess`` and ``time`` are also
# exposed for the same reason — the original silence.py module-imported
# them and some tests go through that namespace.
import queue  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from pathlib import Path

import stream2video.utils as _utils_mod
from stream2video.silence.cache import (
    _get_cache_path,
    _get_wav_cache_path,
    _get_wav_verified_path,
    _is_wav_cache_valid,
    _load_silence_cache_from_path,
    _mark_wav_verified,
    _save_cache,
    clear_wav_verified,
    load_silence_cache,
    save_silence_cache,
)
from stream2video.silence.detect import (
    _extract_audio_wav,
    _run_silencedetect,
    _sample_segments_match,
    detect_silence_stream,
)
from stream2video.silence.parser import (
    _RESUME_THROTTLE_N,
    _RESUME_THROTTLE_S,
    _SAMPLE_VERIFY_DURATION,
    _SEGMENT_MATCH_TOLERANCE,
    _SILENCE_END_RE,
    _SILENCE_START_RE,
    _SILENCE_TIMEOUT,
    SilenceCancelledError,
    SilenceDetectionError,
    SilenceOutOfMemoryError,
    SilenceParser,
    SilenceSegment,
    _noop_on_segment,
    _parse_ffmpeg_output,
    _to_float,
    apply_margin,
)
from stream2video.silence.pipeline import detect_silence
from stream2video.utils import (  # noqa: F401
    drain_stderr_lines,
    no_window_kwargs,
    read_lines_queue,
    registered_process,
)


def _probe_duration(video_path: Path) -> float | None:
    """Alias kept for backward compatibility — historically the silence
    module called ``get_video_duration`` via this name (see the comment
    block in ``silence_pipeline.py``).
    """
    return _utils_mod.get_video_duration(video_path)


def ffmpeg_path() -> str:
    from stream2video import tools as _t

    return _t.ffmpeg_path()


__all__ = [
    "_RESUME_THROTTLE_N",
    "_RESUME_THROTTLE_S",
    "_SAMPLE_VERIFY_DURATION",
    "_SEGMENT_MATCH_TOLERANCE",
    "_SILENCE_END_RE",
    "_SILENCE_START_RE",
    "_SILENCE_TIMEOUT",
    "SilenceCancelledError",
    "SilenceDetectionError",
    "SilenceOutOfMemoryError",
    "SilenceParser",
    "SilenceSegment",
    "_extract_audio_wav",
    "_get_cache_path",
    "_get_wav_cache_path",
    "_get_wav_verified_path",
    "_is_wav_cache_valid",
    "_load_silence_cache_from_path",
    "_mark_wav_verified",
    "_noop_on_segment",
    "_parse_ffmpeg_output",
    "_probe_duration",
    "_run_silencedetect",
    "_sample_segments_match",
    "_save_cache",
    "_to_float",
    "apply_margin",
    "clear_wav_verified",
    "detect_silence",
    "detect_silence_stream",
    "load_silence_cache",
    "save_silence_cache",
]
