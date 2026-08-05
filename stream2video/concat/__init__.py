"""Video cutting and concatenation pipeline dispatch.

This package replaces the historical single-file ``concat.py``. Public
symbols are re-exported from :mod:`stream2video.concat` so every
existing ``from stream2video.concat import X`` keeps working.

Layout::

    stream2video/concat/__init__.py     -- public API
    stream2video/concat/errors.py       -- exception hierarchy
    stream2video/concat/constants.py    -- shared module-level constants
    stream2video/concat/helpers.py      -- pure helpers (audio opts, memory monitor)
    stream2video/concat/encoders.py     -- encoder selection + option builders
    stream2video/concat/manifest.py     -- resume manifest read/write/validate
    stream2video/concat/probing.py      -- ffprobe validity checks
    stream2video/concat/runner.py       -- ``_run_ffmpeg`` subprocess driver
    stream2video/concat/final_concat.py -- concat-demuxer join
    stream2video/concat/audio.py        -- audio-only extract + filter-concat join
    stream2video/concat/gapless.py      -- concat-filter tree for gapless joins
    stream2video/concat/segment.py      -- segment encode pipeline
    stream2video/concat/cut_encode.py   -- cut-then-encode pipeline
    stream2video/concat/batch.py        -- chunked (batch) pipeline
    stream2video/concat/fallback.py     -- libx264 fallback wrapper + dispatcher
    stream2video/concat/api.py          -- ``cut_and_concat`` entry point
"""

#######################################################################
# Re-export every submodule's public surface. Imports inside each
# submodule are packaged as lazy lookups through this namespace so
# ``unittest.mock.patch("stream2video.concat.<name>")`` continues to
# work the way the pre-split test suite expects.
#######################################################################

# Expose for tests that patch low-level subprocess entry points via
# ``stream2video.concat.subprocess.Popen`` (regression coverage for the
# Windows error-206 incident). Importing the modules here lets the
# patch's attribute traversal succeed; the actual runtime work happens
# inside the submodules below.
import queue  # noqa: F401
import subprocess  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401

from stream2video.concat.api import cut_and_concat
from stream2video.concat.audio import _run_audio_concat_filter, _run_audio_extract
from stream2video.concat.batch import _run_batch_concat
from stream2video.concat import constants as _consts

# Surface every constant as an attribute of the package so external
# callers (and ``patch("stream2video.concat.<CONST>")``) keep working.
_AUDIO_BITRATE = _consts._AUDIO_BITRATE
_AUDIO_BITRATES = _consts._AUDIO_BITRATES
_AUDIO_CHANNELS = _consts._AUDIO_CHANNELS
_AUDIO_SAMPLE_RATE = _consts._AUDIO_SAMPLE_RATE
_BATCH_CHUNK_MIN = _consts._BATCH_CHUNK_MIN
_BATCH_CHUNK_SIZE = _consts._BATCH_CHUNK_SIZE
_FINAL_CONCAT_TIMEOUT = _consts._FINAL_CONCAT_TIMEOUT
_MIN_PART_BYTES = _consts._MIN_PART_BYTES
_SEGMENT_ENCODE_TIMEOUT = _consts._SEGMENT_ENCODE_TIMEOUT
_STALL_KILL = _consts._STALL_KILL
_STALL_WARNING = _consts._STALL_WARNING
_STDERR_TRUNCATE = _consts._STDERR_TRUNCATE
_VIDEO_BITRATE = _consts._VIDEO_BITRATE
ENCODER_CHECK_TIMEOUT = _consts.ENCODER_CHECK_TIMEOUT
from stream2video.concat.cut_encode import _run_cut_then_encode
from stream2video.concat.encoders import (
    _VIDEO_BITRATES,
    _X264_CRF,
    ENCODER_OPTS,
    _fps_filter_chain,
    _threads_opt,
    _x264_low_memory_opts,
    check_encoder,
    encoder_opts,
    get_video_encoder,
)
from stream2video.concat.errors import (
    CancelledError,
    ConcatError,
    EncoderUnavailableError,
    FFmpegError,
    FFmpegOutOfMemoryError,
)
from stream2video.concat.fallback import _run_with_fallback, _with_libx264_fallback
from stream2video.concat.final_concat import _run_final_concat
from stream2video.concat.gapless import (
    _concat_filter_one_pass,
    _run_gapless_segment_concat,
)
from stream2video.concat.helpers import (
    _audio_bitrate,
    _audio_bitrate_opts,
    _audio_opts,
    _make_memory_monitor_factory,
    _memory_budget_mb,
    _new_memory_monitor,
    _quote_concat_path,
    generate_keep_segments,
)
from stream2video.concat.manifest import (
    PIPELINE_VERSION,
    _build_manifest,
    _ensure_fresh_work_dir,
    _load_manifest,
    _manifest_path,
    _source_identity,
    _validate_manifest,
    _write_manifest,
)
from stream2video.concat.probing import (
    _ffprobe_duration_ok,
    _ffprobe_is_valid_media,
    _ffprobe_is_valid_mp4,
)
from stream2video.concat.runner import (
    _run_ffmpeg,
    _run_subprocess_cmd,
    _wait_with_cancel,
)
from stream2video.concat.segment import _run_segment_concat


def _effective_cancel_callback_shim(cancel_callback):
    """Pass-through factory so tests patching
    ``stream2video.concat._effective_cancel_callback`` intercept.

    Outside of tests this is the identity function — the runner imports
    the helper directly and calls it once per run.
    """
    return cancel_callback

# ------------------------------------------------------------------
# Indirection layer preserved for monkey-patching tests.
#
# The historical ``concat.py`` module exposed low-level helpers at
# module scope, so tests could patch ``stream2video.concat.ffmpeg_path``
# / ``subprocess.Popen`` / ``drain_stderr_lines`` and have the runner
# honour it. Now that the runner lives in ``concat.runner``, we keep
# thin indirections here so ``patch("stream2video.concat.<name>")``
# continues to intercept the call.
# ------------------------------------------------------------------

import stream2video.tools as _tools_mod  # noqa: E402  (after local imports)
import stream2video.utils as _utils_mod  # noqa: E402


def ffmpeg_path() -> str:
    return _tools_mod.ffmpeg_path()


def ffprobe_path() -> str:
    return _tools_mod.ffprobe_path()


def popen_with_retry(cmd, **kwargs):
    return _tools_mod.popen_with_retry(cmd, **kwargs)


def run_with_retry(cmd, **kwargs):
    return _tools_mod.run_with_retry(cmd, **kwargs)


def drain_stderr_lines(pipe, sink, on_line=None):
    return _utils_mod.drain_stderr_lines(pipe, sink, on_line=on_line)


def read_lines_queue(pipe):
    return _utils_mod.read_lines_queue(pipe)


# Direct passthroughs from ``stream2video.utils`` that tests patch on
# the concat module itself.
from stream2video.utils import (
    get_video_duration,
    get_video_start_time,
    has_audio_stream,
    looks_like_oom,
)

# ------------------------------------------------------------------
# Constants that aren't tied to a specific submodule and that tests
# patch directly. Defined here so ``patch("stream2video.concat.
# _GAPLESS_MAX_INPUTS_PER_CALL", ...)`` keeps working.
# ------------------------------------------------------------------

# Cap on inputs per gapless concat-filter invocation on Windows. Each
# input costs ~len(path)+6 cmdline chars plus ~17 of filter graph; the
# Win32 CreateProcess command line is capped at 32,767 chars total, so a
# few hundred segments would otherwise blow past it (the 2026-08-02/03
# incident: 381 segments → 48K chars → winerror 206). 200 inputs of a
# ~110-char path ≈ 26K chars — safely under. Shrunk by tests to
# exercise the tree without writing hundreds of files.
_GAPLESS_MAX_INPUTS_PER_CALL: int = 200


__all__ = [
    # errors
    "ConcatError",
    "FFmpegError",
    "FFmpegOutOfMemoryError",
    "CancelledError",
    "EncoderUnavailableError",
    # public entry point
    "cut_and_concat",
    "generate_keep_segments",
    "encoder_opts",
    "check_encoder",
    "get_video_encoder",
    "ENCODER_OPTS",
    # historically patch-ed internals
    "_run_ffmpeg",
    "_run_subprocess_cmd",
    "_wait_with_cancel",
    "_run_final_concat",
    "_run_audio_concat_filter",
    "_run_audio_extract",
    "_concat_filter_one_pass",
    "_run_gapless_segment_concat",
    "_run_segment_concat",
    "_run_cut_then_encode",
    "_run_batch_concat",
    "_with_libx264_fallback",
    "_run_with_fallback",
    "_GAPLESS_MAX_INPUTS_PER_CALL",
    "_build_manifest",
    "_write_manifest",
    "_load_manifest",
    "_validate_manifest",
    "_ensure_fresh_work_dir",
    "_manifest_path",
    "_source_identity",
    "_ffprobe_is_valid_media",
    "_ffprobe_is_valid_mp4",
    "_ffprobe_duration_ok",
    "_audio_bitrate",
    "_audio_bitrate_opts",
    "_audio_opts",
    "_fps_filter_chain",
    "_threads_opt",
    "_x264_low_memory_opts",
    "_quote_concat_path",
    "_make_memory_monitor_factory",
    "_memory_budget_mb",
    "_new_memory_monitor",
    "_VIDEO_BITRATES",
    "_X264_CRF",
    "PIPELINE_VERSION",
    # Re-exported utils helpers (historically patched / from-imported
    # through ``stream2video.concat``).
    "get_video_duration",
    "get_video_start_time",
    "has_audio_stream",
    "drain_stderr_lines",
    "read_lines_queue",
    "looks_like_oom",
    "ffmpeg_path",
    "ffprobe_path",
    "popen_with_retry",
    "run_with_retry",
]
