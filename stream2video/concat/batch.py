"""Chunked (batch) pipeline: encode in chunk groups via a single
``filter_complex`` graph per chunk, then join with concat demuxer.

Historically this pipeline traded per-chunk memory for a more expensive
graph build; the current implementation uses ``-filter_complex_script``
to keep the command line under the Windows 32K limit regardless of how
many keep segments the user ends up with.
"""

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stream2video import concat as _c
from stream2video.concat.constants import (
    _BATCH_CHUNK_MIN,
    _BATCH_CHUNK_SIZE,
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
    manifest = _c._build_manifest(
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
        output_fps=output_fps,
        source_has_audio=source_has_audio,
    )
    _c._ensure_fresh_work_dir(batch_dir, manifest)

    # PTS shift handling on sources with a non-zero container
    # ``start_time`` (OBS ``-itsoffset`` captures, mid-file re-muxes):
    # the first frame's PTS is shifted by ``start_time`` seconds.
    #
    # The two timeline spaces involved move in OPPOSITE directions
    # relative to each other:
    #   * ``keep_segments`` (and thus ``chunk_start``/``chunk_end``) are
    #     in *detected* time — the WAV mirror is a plain PCM file whose
    #     timestamps start at 0, so segments are user-visible source
    #     coordinates (verified: silencedetect on a shifted source
    #     reports the same boundaries as on the unshifted one).
    #   * input-side ``-ss`` seeks by *file position*, which is the same
    #     user-visible space — NO compensation needed on the seek.
    #   * ``-copyts`` preserves the shifted PTS into the filter graph,
    #     so the ``trim={s}:{e}`` filters (which match PTS values) must
    #     have their endpoints moved UP by ``start_time`` to land in the
    #     right place; on a clean source (start_time=0) this is a no-op.
    #
    # Empirically verified on ffmpeg 8.1.1 with a 6s source shifted by
    # ``-itsoffset 5``: an uncompensated ``trim=2:4`` with a compensated
    # seek decodes 0 frames (seek lands start_time seconds too early);
    # the formula below (plain seek + PTS-shifted trim) produces the
    # full chunk. Earlier code also subtracted ``start_time`` from the
    # seek — that double compensation truncated or emptied every chunk
    # on shifted sources, and the original "0 frames" report was blamed
    # on the wrong side.
    #
    # Negative start_time is clamped to 0. A negative container
    # start_time (e.g. -2.0 from DTS-based captures) means ffmpeg shifts
    # timestamps so the earliest DTS starts at 0 — the actual PTS
    # timeline IS 0-indexed, and compensating would shift the trim
    # windows early by |start_time|, cutting real content the user wants
    # to keep. ffmpeg's ``-avoid_negative_ts`` at the muxer level already
    # zeroes the DTS side; we just need to not double-compensate here.
    start_time = get_video_start_time(video_path)
    if start_time < 0.0:
        start_time = 0.0

    try:
        encoded_duration = 0.0
        skipped = 0

        for ci, chunk in enumerate(chunks):
            if cancel_callback and cancel_callback():
                raise _c.CancelledError("batch encode cancelled")

            chunk_path = batch_dir / f"chunk_{ci:04d}.mp4"

            # Resume: skip already encoded chunks. Require both a minimum
            # size AND a successful ffprobe read so a crash artifact
            # (missing moov atom) doesn't get reused and produce a
            # corrupt chunk in the middle of the file. When the source
            # has audio, probe the audio stream too — a chunk killed
            # after the moov write but before the AAC body validates as
            # video-but-not-audio and would inject a broken track into
            # the final concat (mirrors the cut_encode.py audio check).
            if (
                chunk_path.exists()
                and chunk_path.stat().st_size >= min_part_bytes
                and _c._ffprobe_is_valid_mp4(chunk_path)
                and (
                    not source_has_audio or _c._ffprobe_is_valid_media(chunk_path, stream_type="a")
                )
                and _c._ffprobe_duration_ok(chunk_path, sum(e - s for s, e in chunk))
            ):
                skipped += 1
                encoded_duration += sum(e - s for s, e in chunk)
                if progress_callback and total_duration > 0:
                    progress_callback(min(encoded_duration / total_duration, 0.9))
                continue

            chunk_start = chunk[0][0]
            chunk_end = chunk[-1][1]
            # Windowed decode. Previously each chunk read the
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
            # ``chunk_start``/``chunk_end`` are user-visible source
            # coordinates and input-side ``-ss`` seeks by file position
            # (the same space) — no start_time adjustment here; only the
            # ``trim`` endpoints below move into ``-copyts`` PTS space
            # (see the comment above ``get_video_start_time``).
            _CHUNK_SEEK_MARGIN = 0.5
            seek_to = max(0.0, chunk_start - _CHUNK_SEEK_MARGIN)
            # Window length must be a pure function of the chunk span +
            # the margins, NOT ``chunk_end - seek_to``: the seek already
            # backs off by the margin, so the old formula decoded up to
            # ``margin`` seconds of unwanted tail per chunk — harmless
            # for output (the ``trim`` below still selects the right
            # frames) but needlessly slow.
            chunk_dur = (chunk_end + _CHUNK_SEEK_MARGIN) - (chunk_start - _CHUNK_SEEK_MARGIN)

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
            fps_suffix = _c._fps_filter_chain(output_fps)
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
                # When ``output_fps != "source"``, splice an
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
                # is video-only.
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
                _c._run_ffmpeg(
                    [
                        ffmpeg_path(),
                        "-y",
                        "-loglevel",
                        "error",
                        "-progress",
                        "pipe:1",
                        # Windowed decode. ``-ss`` before ``-i``
                        # fast-seeks to chunk_start; ``-copyts`` keeps
                        # source PTS so the absolute-time ``trim=...``
                        # filters below still match. ``-t`` must also sit
                        # BEFORE ``-i`` so it is an INPUT option that
                        # stops the demuxer reading once the chunk's
                        # window is past — placed after ``-i`` it would
                        # be an OUTPUT option that only stops the muxer,
                        # leaving the decoder to chew the whole source
                        # per chunk (the windowed-decode win silently
                        # lost on long streams).
                        "-ss",
                        f"{seek_to:.3f}",
                        "-t",
                        f"{chunk_dur:.3f}",
                        "-copyts",
                        "-i",
                        str(video_path),
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
                        # stream".
                        *(
                            [
                                "-map",
                                "[outa]",
                                "-c:a",
                                "aac",
                                *_c._audio_bitrate_opts(audio_quality),
                                *_c._audio_opts(audio_quality),
                            ]
                            if source_has_audio
                            else []
                        ),
                        *(
                            # ``fps`` in the filter graph can
                            # duplicate frames past the keep window's
                            # duration while the audio branch is clamped
                            # by ``atrim=0:{e-s}`` — without ``-shortest``
                            # the muxer writes the longer video tail and
                            # the chunk runs long (frozen tail frames at
                            # every concat join).
                            ["-shortest"] if source_has_audio and output_fps != "source" else []
                        ),
                        str(chunk_path),
                    ],
                    progress_callback=_chunk_prog,
                    timeout=segment_encode_timeout,
                    label=label_text,
                    cancel_callback=cancel_callback,
                    memory_monitor=_c._new_memory_monitor(memory_monitor_factory, label_text),
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

        # Final concat demuxer pass -- shared with _run_segment_concat.
        part_paths = [batch_dir / f"chunk_{ci:04d}.mp4" for ci in range(n_chunks)]
        _c._run_final_concat(
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
            # Same mixed-set seam correction as _run_segment_concat.
            audio_resync=bool(skipped) and source_has_audio,
            audio_quality=audio_quality,
        )
        logger.info(f"batch: successfully created {output_path}")

        # Cleanup on success
        shutil.rmtree(batch_dir, ignore_errors=True)

    except Exception:
        # On failure: keep chunks for resume
        logger.info(f"Chunks kept in {batch_dir} for resume on next run")
        raise
