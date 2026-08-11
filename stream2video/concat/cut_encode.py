"""Cut-then-encode pipeline: stream-copy cuts, lossless concat, ONE
final encode pass.

This is the quality-optimal pipeline (no generation loss between
segments, a single continuous GOP), but sacrifices frame accuracy at
the cut points (``-c copy`` snaps to keyframes).
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from stream2video import concat as _c
from stream2video.concat.constants import (
    _FINAL_CONCAT_TIMEOUT,
    _MIN_PART_BYTES,
    _SEGMENT_ENCODE_TIMEOUT,
    _STALL_KILL,
    _STALL_WARNING,
)
from stream2video.memory import MemoryMonitor
from stream2video.tools import ffmpeg_path
from stream2video.utils import get_video_start_time

logger = logging.getLogger(__name__)


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
    x264_low_memory: bool = False,
) -> None:
    """Cut lossless segments, concat losslessly, then do ONE final encode.

    ``x264_low_memory`` is accepted for symmetry with segment/batch (those
    pipelines use it to size the per-segment bitrate/CRF table). Here the
    flag is currently a no-op — the single final encode uses the caller's
    ``vcodec_opts`` directly — but the parameter must exist so a future
    fix in ``encoder_opts`` keyed on ``x264_low_memory`` silently applies
    to this method too instead of passing a never-forwarded kwarg.

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
        raise _c.ConcatError("No video content to keep (total duration is zero)")

    cut_dir = output_path.parent / f"_{output_path.stem}_cut"
    raw_concat_path = cut_dir / "raw_concat.mkv"

    # Resume manifest (P0.6): same structure as the other methods so
    # a mismatch in source / encoder / keep_segments / pipeline_version
    # wipes the work dir.
    manifest = _c._build_manifest(
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
        output_fps=output_fps,
        source_has_audio=source_has_audio,
    )
    _c._ensure_fresh_work_dir(cut_dir, manifest)
    cut_dir.mkdir(parents=True, exist_ok=True)

    try:
        # start_time compensation (P1.16) — keep_segments are in detected
        # PTS time; input-side -ss positions by file position. See segment.py
        # for the full rationale. No-op on clean sources (start_time=0).
        start_offset = get_video_start_time(video_path)
        if start_offset < 0.0:
            start_offset = 0.0

        # ── Phase 1: Cut pass (stream-copy each segment to MKV) ──
        # Progress: 0.0 .. 0.4 (cut is fast, so a small slice of the bar).
        cut_progress_base = 0.0
        cut_progress_span = 0.4

        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise _c.CancelledError("cut_then_encode cancelled")
            dur = end - start
            cut_path = cut_dir / f"cut_{i:06d}.mkv"

            # Resume skip: if the file exists, is large enough, and
            # passes ffprobe validation, reuse it.
            #
            # Validate BOTH streams (when the source has audio): the cut
            # phase runs ``-c copy`` on video AND audio, so an ffmpeg kill
            # between flushes can leave a chunk with a readable video track
            # but a truncated audio one. The plain video-stream validity
            # check would accept that file, and the lossless phase-2 concat
            # would then splice a broken audio track into the middle of the
            # output. Match ``_ffprobe_is_valid_media(..., "a")`` here the
            # same way ``audio.py:extract`` already does for its segments.
            #
            # Duration check (P0.5): ``-c copy`` snaps the cut start to the
            # nearest *preceding* keyframe, so the actual part length can
            # legitimately exceed ``dur`` by up to one GOP (2-10s on
            # typical streamed H.264). Use a generous 15s slack — large
            # enough to never falsely reject a healthy keyframe-snapped
            # part, small enough to catch a part truncated by a mid-flush
            # kill (the original resume-corruption bug).
            if (
                cut_path.exists()
                and cut_path.stat().st_size >= min_part_bytes
                and _c._ffprobe_is_valid_mp4(cut_path)
                and (not source_has_audio or _c._ffprobe_is_valid_media(cut_path, stream_type="a"))
                and _c._ffprobe_duration_ok(cut_path, dur, slack=15.0)
            ):
                logger.debug(f"cut_then_encode: reusing cut_{i:06d}.mkv")
                continue

            cmd = [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                "-ss",
                # start_time compensation (P1.16) — keep_segments are
                # in detected PTS time; input-side -ss positions by file
                # position. On a source with a non-zero container
                # start_time (OBS -itsoffset) the demuxer's position 0 is
                # PTS start_time, so an uncompensated seek lands start_time
                # seconds too late and the keep segment is clipped early.
                str(max(0.0, start - start_offset)),
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
            _c._run_subprocess_cmd(
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
        if not raw_concat_path.exists() or not _c._ffprobe_is_valid_mp4(raw_concat_path):
            _c._run_final_concat(
                cut_dir,
                raw_concat_path,
                part_paths,
                total_duration=total_duration,
                # _run_final_concat already maps its fraction into the
                # 0.9..1.0 tail; rescale into our 0.4..0.45 slice so the
                # bar moves continuously through the concat step instead
                # of teleporting from 0.4 to 0.49 at its start.
                progress_callback=(
                    (lambda f: progress_callback(0.4 + (f - 0.9) * 0.5))
                    if progress_callback
                    else None
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
                *_c._audio_bitrate_opts(audio_quality),
                *_c._audio_opts(audio_quality),
            ]

        encode_progress_base = 0.5
        encode_progress_span = 0.5

        def _encode_prog(seconds: float) -> None:
            if progress_callback and total_duration > 0:
                frac = min(seconds / total_duration, 1.0)
                progress_callback(encode_progress_base + frac * encode_progress_span)

        label_text = "cut_then_encode final encode"
        _c._run_ffmpeg(
            [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                # -progress pipe:1 is REQUIRED here: _run_ffmpeg's stall
                # watchdog resets on out_time_us lines from stdout, and
                # without -progress a long final encode (multi-hour VOD)
                # emits nothing there and gets killed after ``stall_kill``
                # seconds despite encoding healthily. This regressed every
                # cut_then_encode run longer than the stall window.
                "-progress",
                "pipe:1",
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
            memory_monitor=_c._new_memory_monitor(memory_monitor_factory, label_text),
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
