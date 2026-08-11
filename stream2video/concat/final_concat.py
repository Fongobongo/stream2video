"""Final concat-demuxer join (``-c copy``) shared by all pipelines."""

import logging
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import (
    _FINAL_CONCAT_TIMEOUT,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.memory import MemoryMonitor
from stream2video.tools import ffmpeg_path

logger = logging.getLogger(__name__)


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
            lf.write(f"file {_c._quote_concat_path(part.name)}\n")

    def _concat_prog(seconds: float) -> None:
        if progress_callback and total_duration > 0:
            progress_callback(min(seconds / total_duration * 0.1, 0.1) + 0.9)

    label_text = label
    try:
        _c._run_ffmpeg(
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
            memory_monitor=_c._new_memory_monitor(memory_monitor_factory, label_text),
            stall_kill=stall_kill,
            stall_warning=stall_warning,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
        )
    finally:
        # ``concat.txt`` is a pure-derive artifact of this run. Leaving it
        # behind on failure used to be invisible (the work dir is kept
        # for resume anyway), but if the caller later clears the manifest
        # manually the stale list would silently drive the next concat.
        try:
            list_path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"could not remove concat list {list_path}: {e}")
