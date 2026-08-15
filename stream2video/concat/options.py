"""ConcatOptions — single source of truth for concat tuning knobs.

Every ``_run_*`` pipeline helper (segment / batch / cut_then_encode /
gapless / final concat / audio extract / fallback) used to carry the
same ~20-parameter encoding/tuning block in its signature (audit #12).
They now take one frozen ``ConcatOptions`` instead.

The helpers keep a ``**legacy_kwargs`` shim: the test suite and any
third-party code calling the helpers directly with the old flat kwargs
keep working — ``coerce_options`` folds those kwargs into an options
object (unknown keys raise TypeError, exactly like the old signature).
Production callers pass ``options=`` explicitly and never exercise the
shim.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from stream2video.concat.constants import (
    _BATCH_CHUNK_SIZE,
    _FINAL_CONCAT_TIMEOUT,
    _MIN_PART_BYTES,
    _SEGMENT_ENCODE_TIMEOUT,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.memory import MemoryMonitor

__all__ = ["ConcatOptions", "coerce_options"]

# Legacy kwarg names used by pre-dataclass helper signatures that don't
# match the dataclass field names.
_LEGACY_ALIASES = {
    "timeout": "final_concat_timeout",
}


@dataclasses.dataclass(frozen=True, slots=True)
class ConcatOptions:
    """Encoding + tuning knobs shared by every concat pipeline method.

    One instance is built once by ``_run_locked`` (from the public
    ``cut_and_concat`` kwargs) and threaded down through the fallback /
    method runners — the pre-dataclass code re-listed the same ~20
    parameters in every ``_run_*`` signature.
    """

    encoder: str = "libx264"
    video_quality: str = "medium"
    audio_quality: str = "medium"
    software_fallback: str = "ask"
    fallback_consent: Callable[[], bool] | None = None
    x264_preset: str = "medium"
    encoder_threads: str | int = "auto"
    source_has_audio: bool = True
    output_fps: str = "source"
    x264_low_memory: bool = False
    use_crf: bool = False
    source_bitrate: int | None = None
    gapless_concat: bool = False
    low_process_priority: bool = False
    rlimit_as_mb: int = 0
    segment_encode_timeout: int = _SEGMENT_ENCODE_TIMEOUT
    final_concat_timeout: int = _FINAL_CONCAT_TIMEOUT
    stall_kill: int = _STALL_KILL
    stall_warning: int = _STALL_WARNING
    batch_chunk_size: int = _BATCH_CHUNK_SIZE
    min_part_bytes: int = _MIN_PART_BYTES
    memory_limit_mb: str | int = "auto"
    memory_reserve_mb: int = 2048
    memory_monitor_factory: Callable[[str], MemoryMonitor | None] | None = None

    def replace(self, **kwargs: Any) -> ConcatOptions:
        """Shorthand for ``dataclasses.replace`` (used by the fallback
        path to re-run a method under a different encoder). ``Any`` —
        the values are arbitrary field values, validated by
        ``dataclasses.replace`` / the dataclass constructor at runtime
        (unknown names raise TypeError)."""
        return dataclasses.replace(self, **kwargs)


def coerce_options(
    options: ConcatOptions | None,
    legacy_kwargs: dict,
) -> ConcatOptions:
    """Resolve a helper's ``options`` param, falling back to the old
    flat-kwargs spelling for direct callers (tests, scripts).

    ``legacy_kwargs`` must be empty when ``options`` is given — passing
    both is a programming error and raises.
    """
    if options is not None:
        if legacy_kwargs:
            raise TypeError(
                "Pass either options= or the legacy flat kwargs, not both"
            )
        return options
    if not legacy_kwargs:
        return ConcatOptions()
    remapped = {_LEGACY_ALIASES.get(k, k): v for k, v in legacy_kwargs.items()}
    return ConcatOptions(**remapped)
