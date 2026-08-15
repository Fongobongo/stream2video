"""Cut-then-encode pipeline: encode each keep segment, concat losslessly.

This was historically a quality-optimal pipeline that did stream-copy
cuts (``-c copy``) and ONE final encode pass, but the stream-copy cut
snapped video boundaries to the nearest preceding keyframe, so the
output was up to one GOP longer per segment than the keep window, with
A/V drift on segment boundaries (the audio was cut exactly by ``-t``
but video copied to the next KF). On resume the slack=15.0 validator
silently locked those wrong-length parts into the manifest and the
final concat glued them together, producing a structurally broken
output for which the advertised "best quality" advantage did not hold.

P2 audit fix: cut phase now re-encodes each keep segment with the
player's chosen codec (same technique as ``_run_segment_concat`` —
input-side ``-ss`` performs the seek, ffmpeg decodes from the
preceding keyframe and drops frames until ``start`` automatically so
the cut is frame-accurate; ``-t`` limits both streams to ``dur``).
Phase 3 becomes a ``-c copy`` no-op: the encode already happened in
phase 1, the lossless concat in phase 2, nothing is reencoded in
phase 3.

The historical "quality" win (single continuous GOP) is sacrificed;
functionally this is now equivalent to ``segment`` (per-segment
encode + concat demuxer), differing only in work directory name
(``_<stem>_cut``) and a slightly different progress curve. Kept as a
distinct method so the existing CLI ``--method cut_then_encode`` keeps
working unchanged.
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
    """Encode each keep segment frame-accurately, concat losslessly,
    then stream-copy the result to the final output.

    ``x264_low_memory`` is accepted for symmetry with segment/batch (those
    pipelines use it to size the per-segment bitrate/CRF table). Here the
    flag is currently a no-op — the single final encode uses the caller's
    ``vcodec_opts`` directly — but the parameter must exist so a future
    fix in ``encoder_opts`` keyed on ``x264_low_memory`` silently applies
    to this method too instead of passing a never-forwarded kwarg.

    Unlike ``_run_segment_concat`` (which writes ``seg_*.mp4`` into a
    ``_<stem>_segments`` work dir) and ``_run_batch_concat`` (which writes
    batch chunks into ``_<stem>_batch``), this method does:

      1. **Encode pass**: frame-accurately encode each keep segment
         directly with the chosen codec (``-ss {start} -i src -t
         {dur} -c:v {vcodec} -c:a aac``). Same input-seek technique as
         ``segment.py``: ffmpeg's MP4 demuxer decodes from the preceding
         keyframe and drops frames until ``start`` automatically — so
         the encode is frame-accurate without an extra trim filter.
      2. **Lossless concat**: join all encoded segments into a single
         intermediate MP4 via the concat demuxer (``-c copy``).
      3. **Stream-copy to output**: ``-c copy`` from the intermediate
         into ``output_path``. With phase 1 already producing the
         final codecs there's nothing to encode here — this pass is a
         no-op mux that renames + atomically finalises the output.

    Cut accuracy: frame-accurate (same as ``segment``).

    Resume: same manifest + ffprobe validation as the other methods.
    Work dir: ``_<stem>_cut`` (distinct from ``_segments`` / ``_batch``
    so manifests don't collide).
    """
    n_segs = len(keep_segments)
    total_duration = sum(e - s for s, e in keep_segments)
    if total_duration <= 0:
        raise _c.ConcatError("No video content to keep (total duration is zero)")

    cut_dir = output_path.parent / f"_{output_path.stem}_cut"
    raw_concat_path = cut_dir / "raw_concat.mp4"

    # Resume manifest: same structure as the other methods so
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
        # ``keep_segments`` are in the *detected* timeline that silence
        # detection produced. The WAV mirror is a plain PCM file, so its
        # timestamps start at 0 — even on a source with a non-zero
        # container ``start_time`` (OBS ``-itsoffset``) the detected
        # segments are in user-visible source-time coordinates. Input-side
        # ``-ss`` positions by file position, the same space — no
        # start_time compensation is needed (verified on ffmpeg 8.1.1;
        # see segment.py). The batch path compensates only its ``trim``
        # endpoints, which operate in ``-copyts`` PTS space.

        # ── Phase 1: Cut pass (encode each segment to MP4) ──
        # Progress: 0.0 .. 0.4 (cut is fast, so a small slice of the bar).
        # The per-segment base advances with the cumulative encoded
        # duration (same scheme as segment.py) so the bar never rolls
        # back to zero at the start of a new segment.
        cut_progress_span = 0.4
        encoded_keep = 0.0

        for i, (start, end) in enumerate(keep_segments):
            if cancel_callback and cancel_callback():
                raise _c.CancelledError("cut_then_encode cancelled")
            dur = end - start
            # MP4 (not MKV) parts: the phase-3 mux does ``-c copy`` into
            # ``output_path``, and a MKV intermediate (time_base 1/1000)
            # remuxed into MP4 loses the nominal frame rate — ffprobe then
            # reports r_frame_rate=240/1 for a 30 FPS stream (verified on
            # ffmpeg 8.1.1). MP4 parts keep the MP4→MP4 copy lossless AND
            # frame-rate-preserving, matching what ``segment.py`` does.
            cut_path = cut_dir / f"cut_{i:06d}.mp4"

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
            # Duration check (P2 audit): phase 1 now re-encodes each
            # segment with the chosen codec + input-side ``-ss start``
            # + ``-t dur``, so the output length is exactly ``dur``
            # (frame-accurate, verified with ffmpeg 8.1.1 — same as
            # ``segment.py``'s cut). Slack=1.0 (the module default;
            # matches batch.py / audio.py) is plenty for fractional
            # frame rounding and tight enough to reject a part whose
            # body was truncated by a mid-flush kill. The legacy
            # stream-copy cut used slack=15.0 because ``-c copy``
            # legitimately let the part overrun by up to one GOP —
            # that's no longer the case here.
            if (
                cut_path.exists()
                and cut_path.stat().st_size >= min_part_bytes
                and _c._ffprobe_is_valid_mp4(cut_path)
                and (not source_has_audio or _c._ffprobe_is_valid_media(cut_path, stream_type="a"))
                and _c._ffprobe_duration_ok(cut_path, dur)
            ):
                logger.debug(f"cut_then_encode: reusing cut_{i:06d}.mp4")
                encoded_keep += dur
                if progress_callback and total_duration > 0:
                    progress_callback(
                        min(encoded_keep / total_duration * cut_progress_span, cut_progress_span)
                    )
                continue

            # Frame-accurate encode of one keep segment. Input-side
            # ``-ss {start_pos}`` performs the seek (fast, KF-aligned
            # coarse seek), and the MP4 demuxer decodes from the
            # preceding keyframe then drops frames until ``start`` so
            # the output is frame-accurate on modern ffmpeg. ``-t dur``
            # bounds both video and audio to exactly ``dur`` — no
            # extra ``trim``/``atrim`` filter is needed (the segment
            # method has been doing this; verified on
            # ffmpeg 8.1.1 with a GOP=30 source). The audio re-encode
            # here (instead of the previous stream copy) avoids the
            # KF-snap overrun that broke segment boundaries.
            cmd = [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-ss",
                str(max(0.0, start)),
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
                    "-c:v",
                    vcodec,
                    *vcodec_opts,
                ]
            )
            if source_has_audio:
                cmd.extend(
                    [
                        "-c:a",
                        "aac",
                        *_c._audio_bitrate_opts(audio_quality),
                        *_c._audio_opts(audio_quality),
                    ]
                )
            # FPS conversion: when ``output_fps != 'source'`` the
            # ``fps=`` filter duplicates/drops frames to the target CFR.
            # Apply on the encode side so each segment is independently
            # CFR; the lossless phase-2 concat then joins CFR parts with
            # no PTS jumps. Same logic as segment.py.
            if output_fps != "source":
                cmd.extend(["-vf", f"fps={output_fps}"])
            # Thread count: forward so libx264 (or whatever encoder is
            # chosen) respects the user's low-CPU intent. Mirrors
            # segment.py's thread forwarding.
            cmd.extend(_c._threads_opt(encoder_threads))
            cmd.extend(
                [
                    "-movflags",
                    "+faststart",
                    str(cut_path),
                ]
            )

            # Per-segment progress: the base is the cumulative duration
            # already encoded, so the bar advances monotonically through
            # the whole cut phase (no rollback at each new segment).
            # ``_dur`` / ``_encoded_keep`` are bound as default args so
            # the closure doesn't capture the loop variables (B023).
            def _seg_prog(
                seconds: float, _dur: float = dur, _encoded_keep: float = encoded_keep
            ) -> None:
                if progress_callback and total_duration > 0 and _dur > 0:
                    seg_frac = min(seconds / _dur, 1.0)
                    abs_time = _encoded_keep + seg_frac * _dur
                    progress_callback(
                        min(abs_time / total_duration * cut_progress_span, cut_progress_span)
                    )

            _c._run_ffmpeg(
                cmd,
                progress_callback=_seg_prog if progress_callback else None,
                timeout=segment_encode_timeout,
                label=f"cut_then_encode cut phase segment {i}",
                cancel_callback=cancel_callback,
                memory_monitor=_c._new_memory_monitor(
                    memory_monitor_factory, f"cut_then_encode seg {i}"
                ),
                stall_kill=stall_kill,
                stall_warning=stall_warning,
                low_process_priority=low_process_priority,
                rlimit_as_mb=rlimit_as_mb,
            )
            encoded_keep += dur
            if progress_callback and total_duration > 0:
                progress_callback(
                    min(encoded_keep / total_duration * cut_progress_span, cut_progress_span)
                )

        # ── Phase 2: Lossless concat (concat demuxer → raw_concat.mp4) ──
        # Progress: 0.4 .. 0.5 (fast stream-copy join).
        part_paths = [cut_dir / f"cut_{i:06d}.mp4" for i in range(n_segs)]

        # Reuse _run_final_concat but point it at raw_concat.mp4 instead
        # of the final output. The progress span for this step is small
        # (0.4..0.5) because it's a stream copy.
        # Resume gate: validate BOTH streams, not just video. Phase-2 is a
        # stream-copy join of video+audio; a crash between the AAC body
        # write and the moov finalize leaves a file whose video validates
        # but whose audio is truncated/absent — and the final encode below
        # would silently swallow it, producing an out.mp4 with no sound.
        # segment.py / batch.py already do the dual probe for their parts;
        # this is the same check for the raw concat (mirrors the audio
        # probe on the cut parts earlier in this function).
        #
        # Duration check (audit): a moov-bearing but body-truncated
        # raw_concat.mp4 (ffmpeg killed mid phase-2 write) passes the
        # codec probes above — the moov reflects the PLANNED length while
        # the body is shorter. The phase-3 ``-c copy`` would then rename
        # that truncated file to the final output with no error. Compare
        # the probed duration against the full expected keep duration;
        # slack=1.0 matches the cut-part check above and the other
        # pipelines.
        if not (
            raw_concat_path.exists()
            and raw_concat_path.stat().st_size >= min_part_bytes
            and _c._ffprobe_is_valid_mp4(raw_concat_path)
            and (
                not source_has_audio or _c._ffprobe_is_valid_media(raw_concat_path, stream_type="a")
            )
            and _c._ffprobe_duration_ok(raw_concat_path, total_duration)
        ):
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

        # ── Phase 3: Stream-copy intermediate → final output ──
        # Progress: 0.5 .. 1.0.
        # Phase 1 already encoded every segment with the chosen codec +
        # audio quality, and phase 2 joined them losslessly. There is
        # nothing to re-encode here — phase 3 is now a ``-c copy``
        # mux-level rewrite of the intermediate MP4 into the requested
        # output container. The muxer still touches the whole file
        # (copying the moov + faststart layout), so progress is mapped
        # into 0.5..1.0 from ffmpeg's ``out_time``; it's I/O-bound
        # rather than CPU-bound. ``_run_ffmpeg`` (not the bare
        # ``_run_subprocess_cmd``) is used because it parses the
        # ``-progress pipe:1`` stream into a progress callback AND runs
        # the stall watchdog off it.
        encode_progress_base = 0.5
        encode_progress_span = 0.5

        def _encode_prog(seconds: float) -> None:
            if progress_callback and total_duration > 0:
                frac = min(seconds / total_duration, 1.0)
                progress_callback(encode_progress_base + frac * encode_progress_span)

        label_text = "cut_then_encode mux-to-output"
        _c._run_ffmpeg(
            [
                ffmpeg_path(),
                "-y",
                "-loglevel",
                "error",
                # -progress pipe:1 is required here: _run_ffmpeg parses
                # out_time lines from stdout to drive progress_callback
                # and to reset its stall watchdog timer.
                "-progress",
                "pipe:1",
                "-i",
                str(raw_concat_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            progress_callback=_encode_prog,
            timeout=final_concat_timeout,
            label=label_text,
            cancel_callback=cancel_callback,
            memory_monitor=_c._new_memory_monitor(memory_monitor_factory, "cut_then_encode mux"),
            stall_kill=stall_kill,
            stall_warning=stall_warning,
            low_process_priority=low_process_priority,
            rlimit_as_mb=rlimit_as_mb,
        )
        if progress_callback:
            progress_callback(1.0)
        logger.info(f"Successfully created output (cut_then_encode): {output_path}")

        # Cleanup on success
        shutil.rmtree(cut_dir, ignore_errors=True)

    except Exception:
        logger.info(f"Cut intermediates kept in {cut_dir} for resume on next run")
        raise
