"""Final concat-demuxer join (``-c copy``) shared by all pipelines."""

import logging
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.options import ConcatOptions, coerce_options
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
    options: ConcatOptions | None = None,
    audio_resync: bool = False,
    **legacy_kwargs: object,
) -> None:
    """Build ``concat.txt`` and run the final concat-demuxer pass.

    Shared by ``_run_segment_concat`` and ``_run_batch_concat``.
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

    **Seam resync.** A mixed part set -- some parts resumed
    from an earlier run, some freshly encoded -- can carry slightly
    different audio timebases, so ``-c copy`` joints are audible as
    A/V drift or clipped samples at the seams. The historically-suggested
    ``-async 1`` / ``-vsync cfr`` are *no-ops* here: with stream copy
    there is no encoding to attach them to (``-async`` warns "option is
    deprecated, use aresample" and applies only to encoded audio;
    ``-vsync`` only touches encoded video). The operative fix is to
    re-encode the audio through ``aresample=async=1:first_pts=0``,
    which stretches/compresses the audio to the video timeline and
    re-anchors it at 0, while video stays stream-copied (lossless).
    Callers pass ``audio_resync=True`` only when the part set is mixed
    (resume + fresh) and the source has audio; a fresh-only set shares
    one encode session's timebase and needs no correction.
    """
    options = coerce_options(options, legacy_kwargs)
    list_path = work_dir / "concat.txt"
    with open(list_path, "w", encoding="utf-8") as lf:
        for part in part_paths:
            lf.write(f"file {_c._quote_concat_path(part.name)}\n")

    if audio_resync:
        codec_opts = [
            "-c:v",
            "copy",
            # Re-encode audio only: async=1 fills/removes samples to keep
            # audio locked to the video timeline across seam discontinuities
            # (per-segment AAC priming), first_pts=0 re-anchors the start.
            "-af",
            "aresample=async=1:first_pts=0",
            "-c:a",
            "aac",
            *_c._audio_bitrate_opts(options.audio_quality),
        ]
    else:
        codec_opts = ["-c", "copy"]
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
                *codec_opts,
                str(output_path),
            ],
            progress_callback=_c._concat_progress_callback(progress_callback, total_duration),
            timeout=options.final_concat_timeout,
            label=label_text,
            cancel_callback=cancel_callback,
            memory_monitor=_c._new_memory_monitor(options.memory_monitor_factory, label_text),
            stall_kill=options.stall_kill,
            stall_warning=options.stall_warning,
            low_process_priority=options.low_process_priority,
            rlimit_as_mb=options.rlimit_as_mb,
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
